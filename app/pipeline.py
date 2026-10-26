import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.aggregate.store import muted_categories, save_review
from app.db import connect
from app.github.post import GitHubClient, post_review
from app.orchestrator.graph import Budget, build_graph
from app.orchestrator.llm import make_llm
from app.reliability.audit import write_events
from app.reliability.retry import CircuitBreaker, ResilientGitHub, resilient_llm
from app.reliability.telemetry import traced_llm, traced_review
from app.retrieval.chunk import indexable
from app.retrieval.embed import embed as google_embed
from app.retrieval.search import episodic, index_files, search

MAX_FILES = 40  # per PR: bounds GitHub reads and embedding calls
QUERY_CHARS = 6000  # the diff text used to search; embedding models have an input limit
CONTEXT_CHARS = 30_000


@dataclass
class Deps:
    llm: Callable
    github: Any
    embed: Callable
    connect: Callable = connect
    saver: Any = None  # LangGraph checkpointer (Postgres in production)


def build_deps(env=os.environ, saver=None) -> Deps:
    """Production wiring: model calls are traced, then retried behind a circuit breaker."""
    llm = resilient_llm(traced_llm(make_llm(env)), CircuitBreaker())
    return Deps(llm=llm, github=GitHubClient.from_env(env), embed=lambda texts: google_embed(texts, env=env),
                connect=lambda: connect(env.get("TIGER_DATABASE_URL")), saver=saver)


async def open_saver(env=os.environ):
    """Postgres checkpoints in Tiger Cloud (decision 2.A). Returns (context manager, saver); setup() is idempotent."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    cm = AsyncPostgresSaver.from_conn_string(env["TIGER_DATABASE_URL"])
    saver = await cm.__aenter__()
    await saver.setup()
    return cm, saver


def render_context(chunks: list[dict], past: list[dict], conventions: str) -> str:
    parts = []
    if conventions:
        parts.append("## Team conventions\n" + conventions)
    if chunks:
        parts.append("## Related code\n" + "\n\n".join(f"### {c['path']}\n{c['content']}" for c in chunks))
    if past:
        parts.append("## Earlier findings on these files\n" + "\n".join(
            f"- {p['file']}:{p['line']} {p['severity']} {p['category']}{' (disputed)' if p['disputed'] else ''}: {p['rationale']}"
            for p in past))
    return "\n\n".join(parts)[:CONTEXT_CHARS] or "(no related code found)"


def prepare(deps: Deps, repo: str, number: int, sha: str):
    """Fetch the PR, index the files it touched, and retrieve context. Blocking: run it in a thread."""
    gh = deps.github
    diff = gh.get_pr_diff(repo, number)
    files, removed = {}, []
    for f in gh.get_changed_files(repo, number)[:MAX_FILES]:
        if f["status"] == "removed":
            removed.append(f["filename"])
        elif indexable(f["filename"]):
            text = gh.get_file(repo, f["filename"], sha)
            if text:
                files[f["filename"]] = text
    conventions = gh.get_file(repo, "CONVENTIONS.md", sha) or ""
    conn = deps.connect()
    try:
        index_files(conn, repo, files, deps.embed, removed)
        chunks = search(conn, repo, diff[:QUERY_CHARS], deps.embed)
        past = episodic(conn, repo, list(files))
        muted = muted_categories(conn, repo)
    finally:
        conn.close()
    return diff, render_context(chunks, past, conventions), [c["id"] for c in chunks], muted


def finalize(deps: Deps, repo: str, number: int, sha: str, delivery_id: str, diff: str, out: dict) -> dict:
    """Save the review and its events atomically, then post it unless the gate wants a human first."""
    review = out["review"]
    conn = deps.connect()
    try:
        with conn.transaction():
            existed = conn.execute("SELECT 1 FROM reviews WHERE delivery_id=%s", (delivery_id,)).fetchone()
            review_id = save_review(conn, repo, number, sha, delivery_id, review)
            if not existed:  # a retry must not duplicate the audit trail
                pr_id = conn.execute("SELECT pr_id FROM reviews WHERE id=%s", (review_id,)).fetchone()[0]
                snapshot = {"time": time.time(), "agent": "aggregator", "event_type": "review.snapshot",
                            "review": review}  # lets a human approve later without re-running the models
                write_events(conn, review_id, pr_id, out.get("events", []) + [snapshot])
        github_id = post_review(conn, ResilientGitHub(deps.github), review_id, repo, number, sha, diff, review)
    finally:
        conn.close()
    return {"review_id": review_id, "action": review["action"], "reason": review["reason"],
            "github_review_id": github_id, "cost_usd": out.get("cost_usd", 0.0)}


async def process_pr(deps: Deps, repo: str, number: int, sha: str, delivery_id: str) -> dict:
    """The whole review for one webhook delivery. Safe to retry: indexing is incremental, the graph resumes from
    its checkpoint, saving is idempotent on delivery_id and posting happens at most once."""
    diff, context, context_ids, muted = await asyncio.to_thread(prepare, deps, repo, number, sha)
    graph = build_graph(deps.llm, deps.saver, budget=Budget())
    out = await traced_review(graph, diff, context, delivery_id, muted=muted, context_ids=context_ids)
    return await asyncio.to_thread(finalize, deps, repo, number, sha, delivery_id, diff, out)
