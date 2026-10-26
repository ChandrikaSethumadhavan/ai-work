import json
import os
import uuid

import pytest

from app.aggregate.gate import aggregate, decide, dedupe, overall_confidence
from app.orchestrator.graph import build_graph, run_review
from app.orchestrator.llm import LLMResult
from tests.test_orchestrator import FakeLLM, finding


def f(agent="security", severity="LOW", confidence=0.9, category="x", file="a.py", line=1):
    return finding(agent, severity=severity, confidence=confidence, category=category, file=file, line=line)


def test_dedupe_merges_same_spot_keeps_most_severe():  # AC-6
    merged = dedupe([f("security", "HIGH", 0.7), f("quality", "LOW", 0.95, category="X"), f("docs", file="b.py")])
    assert len(merged) == 2
    top = next(m for m in merged if m["file"] == "a.py")
    assert top["severity"] == "HIGH" and top["confidence"] == 0.95 and top["agents"] == ["security", "quality"]


def test_confidence_is_severity_weighted_mean():  # decision Q1=C
    assert overall_confidence([]) == 1.0
    # CRITICAL (w5, 0.5) and INFO (w1, 1.0): (2.5 + 1) / 6
    assert overall_confidence([f(severity="CRITICAL", confidence=0.5), f(severity="INFO", confidence=1.0, line=2)]) \
        == pytest.approx(3.5 / 6)


def test_gate_post_when_confident():  # AC-7
    assert decide([f(confidence=0.95)], [])["action"] == "post"
    assert decide([], [])["action"] == "post"


def test_gate_low_confidence_goes_to_human():  # AC-7
    d = decide([f(confidence=0.5)], [])
    assert (d["action"], d["reason"]) == ("human", "low_confidence")


def test_critical_escalates_even_at_full_confidence():  # AC-7, NFR-9
    d = decide([f(severity="CRITICAL", confidence=1.0)], [])
    assert (d["action"], d["reason"]) == ("escalate", "critical")


def test_incomplete_review_never_auto_posts():
    d = decide([f(confidence=0.99)], [{"agent": "docs", "error": "down"}])
    assert (d["action"], d["reason"]) == ("human", "incomplete")


def test_muted_category_drops_only_medium_and_below():  # FR-9
    r = aggregate([f(category="nit", severity="LOW"), f(category="nit", severity="HIGH", line=2),
                   f(category="other", line=3)], [], muted={"nit"})
    assert {x["line"] for x in r["findings"]} == {2, 3} and [x["line"] for x in r["muted"]] == [1]


@pytest.mark.asyncio
async def test_graph_ends_with_aggregated_review():
    class Crit(FakeLLM):
        async def __call__(self, model, system, user, effort):
            r = await super().__call__(model, system, user, effort)
            if "security" in system:
                return LLMResult(json.dumps([f("security", "CRITICAL", 1.0)]), 1, 1)
            return r
    out = await run_review(build_graph(Crit()), "diff", "ctx", "g1")
    r = out["review"]
    assert r["action"] == "escalate"
    assert len(r["findings"]) == 1 and r["findings"][0]["severity"] == "CRITICAL"  # same spot: merged
    assert sorted(r["findings"][0]["agents"]) == ["docs", "quality", "security", "tests"]


@pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")
def test_live_save_dispute_and_mute_threshold():  # AC-11, NFR-5
    from app.aggregate.store import dispute, muted_categories, save_review
    from app.db import apply_schema, connect
    conn = connect()
    apply_schema(conn)
    repo = f"test/{uuid.uuid4().hex[:8]}"
    try:
        review = aggregate([f(category="nit", line=i) for i in range(1, 4)], [])
        rid = save_review(conn, repo, 1, "abc", f"d-{repo}", review)
        assert save_review(conn, repo, 1, "abc", f"d-{repo}", review) == rid  # same delivery: no duplicate
        ids = [r[0] for r in conn.execute("SELECT id FROM findings WHERE review_id=%s ORDER BY id", (rid,))]
        dispute(conn, ids[0]); dispute(conn, ids[1])
        assert muted_categories(conn, repo) == set()  # 2 disputes: below the evidence threshold
        dispute(conn, ids[2])
        assert muted_categories(conn, repo) == {"nit"}  # 3 disputes: feedback now applies
        assert conn.execute("SELECT count(*) FROM hitl_items WHERE review_id=%s AND reason='dispute'",
                            (rid,)).fetchone()[0] == 3
    finally:
        conn.execute("DELETE FROM hitl_items WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM findings WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM reviews WHERE pr_id IN (SELECT id FROM prs WHERE repo=%s)", (repo,))
        conn.execute("DELETE FROM prs WHERE repo=%s", (repo,))
