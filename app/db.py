import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn or os.environ["TIGER_DATABASE_URL"], autocommit=True)


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text())


def record_event(conn, agent: str, event_type: str, *, review_id=None, pr_id=None, cost_usd=0,
                 latency_ms=None, tokens_in=None, tokens_out=None, model=None,
                 prompt_version=None, context_ids=None, payload=None) -> None:
    conn.execute(
        "INSERT INTO agent_events (agent, event_type, review_id, pr_id, cost_usd, latency_ms, tokens_in,"
        " tokens_out, model, prompt_version, context_ids, payload)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (agent, event_type, review_id, pr_id, cost_usd, latency_ms, tokens_in, tokens_out, model,
         prompt_version, context_ids, psycopg.types.json.Jsonb(payload or {})))
