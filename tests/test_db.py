import os
import re

import pytest

from app.db import SCHEMA, apply_schema, connect, record_event

SQL = SCHEMA.read_text()


def test_extensions_and_single_store():  # FR-10
    for ext in ("timescaledb", "vector", "vectorscale"):
        assert f"CREATE EXTENSION IF NOT EXISTS {ext}" in SQL


def test_review_truth_tables_and_finding_contract():  # FR-10
    for table in ("prs", "reviews", "findings", "hitl_items", "code_chunks", "agent_events"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", SQL)
    findings = SQL.split("CREATE TABLE IF NOT EXISTS findings")[1].split(");")[0]
    for col in ("agent_type", "severity", "category", "file", "line", "confidence", "rationale"):
        assert re.search(rf"\b{col}\b", findings)
    assert "'security','quality','tests','docs'" in findings and "'CRITICAL'" in findings


def test_code_chunks_256dim_diskann_and_fts():  # FR-10, AC-10
    assert "vector(256)" in SQL
    assert "USING diskann (embedding vector_cosine_ops)" in SQL
    assert "USING gin (tsv)" in SQL
    assert "UNIQUE (repo, path, content_hash)" in SQL


def test_events_hypertable_one_day_chunks():  # FR-11, AC-9
    assert re.search(r"create_hypertable\('agent_events', 'time', chunk_time_interval => INTERVAL '1 day'", SQL)
    for col in ("prompt_version", "context_ids", "cost_usd", "latency_ms"):
        assert col in SQL.split("agent_events (")[1].split(");")[0]


def test_continuous_aggregates_with_policies():  # FR-12, AC-9
    for view in ("agent_health_1m", "pr_cost_hourly"):
        assert re.search(rf"CREATE MATERIALIZED VIEW IF NOT EXISTS {view}\s+WITH \(timescaledb.continuous\)", SQL)
        assert f"add_continuous_aggregate_policy('{view}'" in SQL


@pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")
def test_live_schema_and_rollups():  # AC-9 (run against Tiger Cloud)
    conn = connect()
    apply_schema(conn)
    apply_schema(conn)  # idempotent
    record_event(conn, "security", "llm.call", pr_id=1, cost_usd=0.018, latency_ms=1200)
    conn.execute("CALL refresh_continuous_aggregate('agent_health_1m', NULL, NULL)")
    conn.execute("CALL refresh_continuous_aggregate('pr_cost_hourly', NULL, NULL)")
    assert conn.execute("SELECT count(*) FROM agent_health_1m").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM pr_cost_hourly").fetchone()[0] >= 1
    assert conn.execute("SELECT count(*) FROM timescaledb_information.dimensions "
                        "WHERE hypertable_name='agent_events' AND time_interval = INTERVAL '1 day'").fetchone()[0] == 1
