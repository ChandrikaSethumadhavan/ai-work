import asyncio
import json
import os
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langgraph.checkpoint.memory import MemorySaver

from app import cli
from app.github.post import GitHubClient
from app.ingress import worker as worker_mod
from app.orchestrator.graph import build_graph, run_review
from app.orchestrator.llm import LLMResult
from app.pipeline import Deps, process_pr, render_context
from app.retrieval.chunk import chunk_file, indexable
from tests.test_orchestrator import Crash, FakeLLM, finding
from tests.test_retrieval import fake_embed

needs_db = pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")
needs_all = pytest.mark.skipif(not (os.environ.get("TIGER_DATABASE_URL") and os.environ.get("REDIS_URL")),
                               reason="needs Tiger Cloud and Redis")

# ---------------------------------------------------------------- chunking

def test_chunks_split_at_definitions_and_respect_the_size_cap():
    code = "import os\n\n" + "".join(f"def f{i}():\n" + "    x = 1\n" * 40 + "\n" for i in range(6))
    chunks = chunk_file(code, max_chars=600)
    assert len(chunks) >= 6 and all(len(c) <= 1200 for c in chunks)
    assert any(c.startswith("def f3()") for c in chunks)  # a chunk starts at a definition


def test_binary_oversized_and_tiny_files_give_no_chunks_and_junk_paths_are_skipped():
    assert chunk_file("abc\x00def" * 20) == [] and chunk_file("x" * 300_000) == [] and chunk_file("hi") == []
    assert indexable("app/main.py") and not indexable("node_modules/x/index.js") and not indexable("logo.png")
    assert not indexable("package-lock.json") and not indexable("static/app.min.js")


# ---------------------------------------------------------------- GitHub reads

def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def gh_client(handler):
    return GitHubClient("123", rsa_pem(), "456", transport=httpx.MockTransport(handler))


def test_github_token_is_cached_and_reads_use_the_right_endpoints():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.headers.get("accept")))
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "tok"})
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path.endswith("/pulls/7"):
            return httpx.Response(200, text="DIFF")
        if "/contents/src/a%20b.py" in req.url.raw_path.decode():  # the space must be URL-encoded
            return httpx.Response(200, text="code")
        return httpx.Response(404)
    gh = gh_client(handler)
    assert gh.get_pr_diff("o/r", 7) == "DIFF"
    assert gh.get_file("o/r", "src/a b.py", "sha") == "code"
    assert gh.get_file("o/r", "missing.md", "sha") is None  # 404 means "not there", not an error
    assert sum(c[1].endswith("/access_tokens") for c in calls) == 1  # one token for three reads
    assert calls[1][2] == "application/vnd.github.diff"


def test_changed_files_paginates_and_from_env_reads_the_key_file(tmp_path):
    pages = []

    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t"})
        page = int(req.url.params["page"]); pages.append(page)
        n = 100 if page == 1 else 20
        return httpx.Response(200, json=[{"filename": f"f{page}-{i}", "status": "modified", "extra": 1} for i in range(n)])
    files = gh_client(handler).get_changed_files("o/r", 1)
    assert pages == [1, 2] and len(files) == 120 and set(files[0]) == {"filename", "status"}
    pem = tmp_path / "k.pem"
    pem.write_text(rsa_pem())
    c = GitHubClient.from_env({"GITHUB_APP_ID": "1", "GITHUB_INSTALLATION_ID": "2", "GITHUB_APP_PRIVATE_KEY_PATH": str(pem)})
    assert (c.app_id, c.installation_id) == ("1", "2") and "PRIVATE KEY" in c.key


def test_render_context_orders_sections_and_caps_size():
    text = render_context([{"path": "a.py", "content": "code"}],
                          [{"file": "a.py", "line": 3, "severity": "LOW", "category": "x", "rationale": "r", "disputed": True}], "be kind")
    assert text.index("conventions") < text.index("Related code") < text.index("Earlier findings") and "(disputed)" in text
    assert render_context([], [], "") == "(no related code found)"
    assert len(render_context([{"path": "p", "content": "x" * 100_000}], [], "")) <= 30_000


# ---------------------------------------------------------------- graph reuse, worker

@pytest.mark.asyncio
async def test_finished_review_is_reused_not_rerun_on_retry():  # a later step failed: do not pay for the models twice
    llm, saver = FakeLLM(), MemorySaver()
    graph = build_graph(llm, saver)
    first = await run_review(graph, "d", "c", "w1")
    calls = len(llm.calls)
    again = await run_review(graph, "d", "c", "w1")
    assert len(llm.calls) == calls and again["review"] == first["review"] and len(again["findings"]) == len(first["findings"])


@pytest.mark.asyncio
async def test_worker_passes_the_job_id_as_the_delivery_id(monkeypatch):
    seen = {}

    async def fake_process(deps, repo, pr, sha, delivery):
        seen.update(deps=deps, args=(repo, pr, sha, delivery))
        return {"ok": True}
    monkeypatch.setattr("app.pipeline.process_pr", fake_process)
    assert await worker_mod.review_pr({"deps": "D", "job_id": "delivery-9"}, "o/r", 4, "abc") == {"ok": True}
    assert seen == {"deps": "D", "args": ("o/r", 4, "abc", "delivery-9")}
    S = worker_mod.WorkerSettings
    from arq.worker import func
    assert [func(f).name for f in S.functions] == ["review_pr"]  # the name ARQ will look the queued job up by
    assert S.max_tries == 3 and S.job_timeout > 0


# ---------------------------------------------------------------- live: pipeline on Tiger Cloud

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 import os
+def risky(x): return eval(x)
 y = 1
"""
A_PY = "import os\ndef risky(x): return eval(x)\ny = 1\n"


class FakeGitHub:
    def __init__(self):
        self.posts = []
        self.reads = 0

    def get_pr_diff(self, repo, number):
        self.reads += 1
        return DIFF

    def get_changed_files(self, repo, number):
        return [{"filename": "a.py", "status": "modified"}, {"filename": "old.py", "status": "removed"},
                {"filename": "logo.png", "status": "added"}]

    def get_file(self, repo, path, ref):
        return {"a.py": A_PY, "CONVENTIONS.md": "Prefer small functions."}.get(path)

    def create_review(self, repo, number, commit_id, body, comments):
        self.posts.append((repo, number, commit_id, body, comments))
        return 9000 + len(self.posts)


class CriticalLLM(FakeLLM):
    async def __call__(self, model, system, user, effort):
        r = await super().__call__(model, system, user, effort)
        if "security" in system:
            return LLMResult(json.dumps([finding("security", severity="CRITICAL", confidence=1.0, line=2)]), 1, 1)
        return r


def cleanup(conn, repo):
    sel = "(SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)"
    for table in ("agent_events", "hitl_items", "findings"):
        conn.execute(f"DELETE FROM {table} WHERE review_id IN {sel}", (repo,))
    conn.execute("DELETE FROM reviews WHERE pr_id IN (SELECT id FROM prs WHERE repo=%s)", (repo,))
    conn.execute("DELETE FROM prs WHERE repo=%s", (repo,))
    conn.execute("DELETE FROM code_chunks WHERE repo=%s", (repo,))


def live_deps(llm, gh):
    from app.db import connect
    return Deps(llm=llm, github=gh, embed=fake_embed, connect=connect, saver=MemorySaver())


@pytest.fixture
def live():
    from app.db import apply_schema, connect
    conn = connect()
    apply_schema(conn)
    repo = f"test/{uuid.uuid4().hex[:8]}"
    yield conn, repo, f"d-{repo}"
    cleanup(conn, repo)


@needs_db
@pytest.mark.asyncio
async def test_live_confident_review_is_saved_audited_and_posted_exactly_once(live):  # FR-3, FR-8, NFR-5
    conn, repo, delivery = live
    gh, llm = FakeGitHub(), FakeLLM()
    deps = live_deps(llm, gh)
    r1 = await process_pr(deps, repo, 5, "sha1", delivery)
    assert r1["action"] == "post" and r1["github_review_id"] == 9001 and len(gh.posts) == 1
    assert gh.posts[0][4] and gh.posts[0][4][0]["path"] == "a.py"  # the finding became an inline comment
    paths = {p for (p,) in conn.execute("SELECT path FROM code_chunks WHERE repo=%s", (repo,))}
    assert paths == {"a.py"}  # the PR's file was indexed; the png and the removed file were not
    events = conn.execute("SELECT count(*) FROM agent_events WHERE review_id=%s", (r1["review_id"],)).fetchone()[0]
    assert events >= 5  # 4 model calls + the snapshot
    calls = len(llm.calls)
    r2 = await process_pr(deps, repo, 5, "sha1", delivery)  # the job is retried
    assert r2["review_id"] == r1["review_id"] and r2["github_review_id"] == 9001
    assert len(gh.posts) == 1 and len(llm.calls) == calls  # no second post, no second model bill
    assert conn.execute("SELECT count(*) FROM agent_events WHERE review_id=%s", (r1["review_id"],)).fetchone()[0] == events
    assert conn.execute("SELECT count(*) FROM reviews WHERE delivery_id=%s", (delivery,)).fetchone()[0] == 1


@needs_db
@pytest.mark.asyncio
async def test_live_critical_review_waits_for_a_human_who_can_approve_it(live):  # FR-7, NFR-9
    conn, repo, delivery = live
    gh = FakeGitHub()
    r = await process_pr(live_deps(CriticalLLM(), gh), repo, 6, "sha2", delivery)
    assert (r["action"], r["reason"], r["github_review_id"]) == ("escalate", "critical", None) and not gh.posts
    lines = []
    cli.run(["list"], conn=conn, out=lines.append)
    mine = [l for l in lines if f"{repo}#6" in l]
    assert len(mine) == 1 and "critical" in mine[0]
    item_id = int(mine[0].split("\t")[0])
    shown = []
    cli.run(["show", str(item_id)], conn=conn, out=shown.append)
    assert any("CRITICAL" in l and "a.py:2" in l for l in shown)
    done = []
    cli.run(["approve", str(item_id), "--by", "chandrika"], conn=conn, github=gh, out=done.append)
    assert len(gh.posts) == 1 and "approved by chandrika" in done[0]
    assert conn.execute("SELECT decision, decided_by FROM hitl_items WHERE id=%s", (item_id,)).fetchone() == ("approved", "chandrika")
    assert conn.execute("SELECT status FROM reviews WHERE id=%s", (r["review_id"],)).fetchone()[0] == "posted"
    with pytest.raises(SystemExit, match="already approved"):
        cli.run(["approve", str(item_id), "--by", "x"], conn=conn, github=gh, out=print)
    assert len(gh.posts) == 1


@needs_db
@pytest.mark.asyncio
async def test_live_rejecting_posts_nothing_and_disputes_are_acknowledged(live):  # FR-9
    from app.aggregate.store import dispute
    conn, repo, delivery = live
    gh = FakeGitHub()
    r = await process_pr(live_deps(CriticalLLM(), gh), repo, 7, "sha3", delivery)
    item_id = conn.execute("SELECT id FROM hitl_items WHERE review_id=%s", (r["review_id"],)).fetchone()[0]
    cli.run(["reject", str(item_id), "--by", "chandrika"], conn=conn, out=lambda _: None)
    assert not gh.posts and conn.execute("SELECT status FROM reviews WHERE id=%s", (r["review_id"],)).fetchone()[0] == "rejected"
    fid = conn.execute("SELECT id FROM findings WHERE review_id=%s LIMIT 1", (r["review_id"],)).fetchone()[0]
    dispute(conn, fid)
    dispute_item = conn.execute("SELECT id FROM hitl_items WHERE review_id=%s AND reason='dispute'", (r["review_id"],)).fetchone()[0]
    cli.run(["approve", str(dispute_item), "--by", "chandrika"], conn=conn, github=gh, out=lambda _: None)
    assert conn.execute("SELECT decision FROM hitl_items WHERE id=%s", (dispute_item,)).fetchone()[0] == "acknowledged"
    assert not gh.posts  # acknowledging a dispute never posts


@needs_db
def test_live_indexing_drops_stale_chunks_and_embeds_only_what_changed(live):  # decision Q6
    from app.retrieval.search import index_files
    conn, repo, _ = live
    v1 = "def a():\n    return 1\n\n" + "def b():\n    return 2\n" * 3
    first = index_files(conn, repo, {"m.py": v1, "gone.py": "def old():\n    return 0\n"}, fake_embed)
    assert first >= 2
    assert index_files(conn, repo, {"m.py": v1}, fake_embed) == 0  # nothing changed: nothing embedded
    v2 = v1.replace("return 1", "return 100")
    assert index_files(conn, repo, {"m.py": v2}, fake_embed, removed=["gone.py"]) >= 1
    texts = [t for (t,) in conn.execute("SELECT content FROM code_chunks WHERE repo=%s", (repo,))]
    assert any("return 100" in t for t in texts) and not any("return 1\n" in t + "\n" and "return 100" not in t for t in texts if "def a" in t)
    assert {p for (p,) in conn.execute("SELECT path FROM code_chunks WHERE repo=%s", (repo,))} == {"m.py"}  # gone.py dropped


@needs_db
@pytest.mark.asyncio
async def test_live_postgres_checkpoints_resume_a_crashed_review():  # AC-4 on the real store (decision 2.A)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    thread = f"ckpt-{uuid.uuid4().hex[:8]}"
    async with AsyncPostgresSaver.from_conn_string(os.environ["TIGER_DATABASE_URL"]) as saver:
        await saver.setup()
        try:
            llm = FakeLLM(crash={"tests"})
            graph = build_graph(llm, saver)
            with pytest.raises(Crash):
                await run_review(graph, "d", "c", thread)
            first = len(llm.calls)
            out = await run_review(build_graph(llm, saver), "d", "c", thread)  # a "new worker" on the same checkpoints
            assert len(llm.calls) - first == 1 and llm.calls[-1][0] == "tests"
            assert out["review"]["action"] == "post"
        finally:
            await saver.adelete_thread(thread)


# ---------------------------------------------------------------- live: webhook -> Redis -> worker -> Tiger

@needs_all
@pytest.mark.asyncio
async def test_live_end_to_end_signed_webhook_through_redis_to_a_saved_review(live):  # AC-1, AC-3, FR-1, FR-3
    import hashlib
    import hmac
    from arq import create_pool
    from arq.worker import Worker
    from app.ingress.main import create_app
    from app.ingress.worker import redis_settings
    conn, repo, delivery = live
    queue = f"arq:test-{uuid.uuid4().hex[:8]}"
    pool = await create_pool(redis_settings(), default_queue_name=queue)
    gh = FakeGitHub()
    payload = json.dumps({"action": "opened", "repository": {"full_name": repo},
                          "pull_request": {"number": 11, "head": {"sha": "sha-e2e"}}}).encode()
    sig = "sha256=" + hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
    try:
        app = create_app("secret", pool)
        async with app.router.lifespan_context(app):  # same event loop as the Redis pool
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/webhook", content=payload, headers={
                    "X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery})
        assert resp.status_code == 200 and resp.json()["status"] == "queued"
        worker = Worker(functions=worker_mod._functions(), redis_settings=redis_settings(), queue_name=queue, burst=True,
                        ctx={"deps": live_deps(FakeLLM(), gh)}, handle_signals=False, poll_delay=0.1, max_tries=3)
        await worker.async_run()
        assert worker.jobs_complete == 1 and worker.jobs_failed == 0
        row = conn.execute("SELECT status, github_review_id FROM reviews WHERE delivery_id=%s", (delivery,)).fetchone()
        assert row == ("posted", 9001) and len(gh.posts) == 1
        await worker.close()
    finally:
        await pool.delete(queue, *[f"arq:{k}:{delivery}" for k in ("job", "result", "retry", "in-progress")])
        await pool.aclose()
