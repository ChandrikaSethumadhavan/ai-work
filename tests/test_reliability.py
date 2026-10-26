import asyncio
import json
import os
import uuid

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.orchestrator import graph as graph_mod
from app.orchestrator.graph import build_graph, run_review
from app.orchestrator.llm import LLMResult
from app.reliability.dead_letter import DEAD_KEY, with_dead_letter
from app.reliability.retry import CircuitBreaker, CircuitOpen, ResilientGitHub, resilient_llm, with_retry
from app.reliability.telemetry import setup_tracing, traced_llm, traced_review
from tests.test_orchestrator import FakeLLM, finding


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def no_sleep(_):
    pass


def flaky(times, ok="ok"):
    state = {"n": 0}

    async def fn():
        state["n"] += 1
        if state["n"] <= times:
            raise RuntimeError("boom")
        return ok
    fn.state = state
    return fn


@pytest.mark.asyncio
async def test_retries_with_exponential_backoff():  # NFR-6
    waits = []

    async def rec(d):
        waits.append(d)
    fn = flaky(2)
    assert await with_retry(fn, tries=3, base=1.0, sleep=rec) == "ok"
    assert waits == [1.0, 2.0] and fn.state["n"] == 3


@pytest.mark.asyncio
async def test_gives_up_after_three_tries():
    fn = flaky(99)
    with pytest.raises(RuntimeError):
        await with_retry(fn, tries=3, sleep=no_sleep)
    assert fn.state["n"] == 3


@pytest.mark.asyncio
async def test_breaker_opens_after_5_failures_then_recovers_after_cooldown():  # NFR-6
    clk, br = Clock(), None
    br = CircuitBreaker(failures=5, window=60, cooldown=30, clock=clk)
    fn = flaky(99)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await with_retry(fn, tries=1, breaker=br)
    calls = fn.state["n"]
    with pytest.raises(CircuitOpen):
        await with_retry(fn, tries=3, breaker=br, sleep=no_sleep)  # refused, not even attempted
    assert fn.state["n"] == calls
    clk.t = 31  # cooldown over: one trial call is allowed
    fn.state["n"] = 100  # now succeeds
    assert await with_retry(fn, tries=1, breaker=br) == "ok"


@pytest.mark.asyncio
async def test_breaker_ignores_old_failures():
    clk = Clock()
    br = CircuitBreaker(failures=3, window=60, cooldown=30, clock=clk)
    for t in (0, 100, 200, 300):  # spread out: never 3 inside one minute
        clk.t = t
        br.failure()
    br.before()  # still closed


@pytest.mark.asyncio
async def test_agent_survives_transient_provider_errors_in_the_graph():  # Q2=A
    llm = FakeLLM()
    calls = {"n": 0}

    async def sometimes(model, system, user, effort):
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise RuntimeError("503")
        return await llm(model, system, user, effort)
    wrapped = resilient_llm(sometimes, CircuitBreaker(), tries=3, base=0, sleep=no_sleep)
    out = await run_review(build_graph(wrapped), "d", "c", "r1")
    assert not out.get("failures") and len(out["findings"]) == 4


@pytest.mark.asyncio
async def test_open_breaker_degrades_gracefully_not_crashes():  # NFR-6
    br = CircuitBreaker(failures=1)
    br.failure()  # open
    out = await run_review(build_graph(resilient_llm(FakeLLM(), br, sleep=no_sleep)), "d", "c", "r2")
    assert len(out["failures"]) == 4 and "CircuitOpen" in out["failures"][0]["error"]


@pytest.mark.asyncio
async def test_slow_agent_times_out_and_is_recorded(monkeypatch):  # NFR-6 timeouts on every node
    monkeypatch.setattr(graph_mod, "AGENT_TIMEOUT", 0.05)

    async def slow(model, system, user, effort):
        await asyncio.sleep(1)
    out = await run_review(build_graph(slow), "d", "c", "r3")
    assert len(out["failures"]) == 4 and "TimeoutError" in out["failures"][0]["error"]


def test_github_calls_retry_and_share_a_breaker():
    class Gh:
        n = 0

        def create_review(self, *a):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("502")
            return 77
    gh = Gh()
    assert ResilientGitHub(gh, base=0, sleep=lambda _: None).create_review("r", 1, "sha", "b", []) == 77 and gh.n == 2


class FakeRedis:
    def __init__(self):
        self.items = []

    async def lpush(self, key, value):
        self.items.append((key, json.loads(value)))


@pytest.mark.asyncio
async def test_failed_job_is_retried_with_backoff_then_dead_lettered_on_the_last_try():  # NFR-6
    from arq import Retry

    async def job(ctx, repo, pr):
        raise RuntimeError("always")
    wrapped = with_dead_letter(job, max_tries=3, base=5)
    redis = FakeRedis()
    for attempt, wait in ((1, 5), (2, 10)):  # ARQ only retries a job that raises Retry
        with pytest.raises(Retry) as r:
            await wrapped({"redis": redis, "job_try": attempt, "job_id": "d1"}, "o/r", 5)
        assert r.value.defer_score == wait * 1000
    assert redis.items == []  # not parked yet
    with pytest.raises(RuntimeError):
        await wrapped({"redis": redis, "job_try": 3, "job_id": "d1"}, "o/r", 5)
    assert len(redis.items) == 1
    key, item = redis.items[0]
    assert key == DEAD_KEY and item["args"] == ["o/r", 5] and item["job_id"] == "d1" and "always" in item["error"]


@pytest.mark.asyncio
async def test_events_carry_prompt_version_context_ids_and_cost():  # NFR-7
    out = await run_review(build_graph(FakeLLM()), "d", "c", "r4", context_ids=[11, 12])
    calls = [e for e in out["events"] if e["event_type"] == "llm.call"]
    assert len(calls) == 4 and all(len(e["prompt_version"]) == 12 and e["context_ids"] == [11, 12] for e in calls)
    assert len({e["prompt_version"] for e in calls}) == 4  # each specialist has its own prompt


@pytest.mark.asyncio
async def test_malformed_output_still_records_its_cost():
    class Bad(FakeLLM):
        async def __call__(self, model, system, user, effort):
            await super().__call__(model, system, user, effort)
            return LLMResult("not json", 1_000_000, 0)
    out = await run_review(build_graph(Bad()), "d", "c", "r5")
    assert len(out["failures"]) == 4 and out["cost_usd"] == pytest.approx(4 + 2 + 2 + 2)  # paid calls are counted
    assert sum(e["event_type"] == "llm.call" for e in out["events"]) == 4


@pytest.mark.asyncio
async def test_otel_spans_record_model_prompt_version_and_review():
    exporter = InMemorySpanExporter()
    setup_tracing(exporter)
    llm = traced_llm(FakeLLM())
    out = await traced_review(build_graph(llm), "d", "c", "r6")
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert names.count("llm.call") == 4 and names.count("review") == 1
    call = next(s for s in spans if s.name == "llm.call")
    assert call.attributes["llm.model"] and len(call.attributes["llm.prompt_version"]) == 12
    review = next(s for s in spans if s.name == "review")
    assert review.attributes["review.delivery_id"] == "r6" and review.attributes["review.cost_usd"] == out["cost_usd"]
    assert all(c.parent.span_id == review.context.span_id for c in spans if c.name == "llm.call")  # nested in review


needs_db = pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")


@needs_db
@pytest.mark.asyncio
async def test_live_audit_trail_reconstructs_context_prompt_and_cost():  # AC-12
    from app.aggregate.store import save_review
    from app.db import apply_schema, connect
    from app.reliability.audit import audit_trail, write_events
    from app.retrieval.search import upsert_chunks
    from tests.test_retrieval import fake_embed
    conn = connect()
    apply_schema(conn)
    repo = f"test/{uuid.uuid4().hex[:8]}"
    try:
        upsert_chunks(conn, repo, [("auth.py", "def verify_token(): pass"), ("db.py", "def insert(): pass")], fake_embed)
        ids = [r[0] for r in conn.execute("SELECT id FROM code_chunks WHERE repo=%s ORDER BY id", (repo,))]
        out = await run_review(build_graph(FakeLLM(tokens=(1_000_000, 0))), "diff", "ctx", f"d-{repo}", context_ids=ids)
        rid = save_review(conn, repo, 3, "sha", f"d-{repo}", out["review"])
        pr_id = conn.execute("SELECT pr_id FROM reviews WHERE id=%s", (rid,)).fetchone()[0]
        write_events(conn, rid, pr_id, out["events"])
        fid = conn.execute("SELECT id FROM findings WHERE review_id=%s", (rid,)).fetchone()[0]
        trail = audit_trail(conn, fid)
        assert [c["path"] for c in trail["context_chunks"]] == ["auth.py", "db.py"]  # what the agent saw
        assert len(trail["prompt_versions"]) == 1 and trail["models"]
        assert trail["cost_usd"] > 0 and trail["finding"]["file"] == "a.py"
    finally:
        conn.execute("DELETE FROM agent_events WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM hitl_items WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM findings WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM reviews WHERE pr_id IN (SELECT id FROM prs WHERE repo=%s)", (repo,))
        conn.execute("DELETE FROM prs WHERE repo=%s", (repo,))
        conn.execute("DELETE FROM code_chunks WHERE repo=%s", (repo,))
