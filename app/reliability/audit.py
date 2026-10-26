import json

from psycopg.types.json import Jsonb


def write_events(conn, review_id: int, pr_id: int, events: list[dict]) -> None:
    """Persist graph events to the agent_events hypertable, keeping their original timestamps (FR-11)."""
    for e in events:
        extra = {k: v for k, v in e.items() if k not in (
            "time", "agent", "event_type", "model", "cost_usd", "tokens_in", "tokens_out", "latency_ms",
            "prompt_version", "context_ids")}
        conn.execute(
            "INSERT INTO agent_events (time, review_id, pr_id, agent, event_type, cost_usd, latency_ms, tokens_in,"
            " tokens_out, model, prompt_version, context_ids, payload)"
            " VALUES (to_timestamp(%s),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (e["time"], review_id, pr_id, e["agent"], e["event_type"], e.get("cost_usd", 0), e.get("latency_ms"),
             e.get("tokens_in"), e.get("tokens_out"), e.get("model"), e.get("prompt_version"),
             e.get("context_ids"), Jsonb(json.loads(json.dumps(extra, default=str)))))


def audit_trail(conn, finding_id: int) -> dict:
    """AC-12: for one finding, reconstruct what the agent saw (retrieved chunks), which prompt version and model
    ran, and what it cost, from the events of the agents that raised it."""
    f = conn.execute("SELECT review_id, agent_type, severity, category, file, line FROM findings WHERE id=%s",
                     (finding_id,)).fetchone()
    events = conn.execute(
        "SELECT agent, model, prompt_version, context_ids, cost_usd FROM agent_events"
        " WHERE review_id=%s AND agent=%s AND event_type='llm.call' ORDER BY time", (f[0], f[1])).fetchall()
    chunk_ids = sorted({i for e in events for i in (e[3] or [])})
    chunks = [{"id": r[0], "path": r[1]} for r in conn.execute(
        "SELECT id, path FROM code_chunks WHERE id = ANY(%s) ORDER BY id", (chunk_ids,))] if chunk_ids else []
    return {"finding": dict(zip(("review_id", "agent", "severity", "category", "file", "line"), f)),
            "models": sorted({e[1] for e in events}), "prompt_versions": sorted({e[2] for e in events if e[2]}),
            "context_chunks": chunks, "cost_usd": float(sum(e[4] for e in events))}
