-- Tiger Cloud (managed Postgres): TimescaleDB + pgvector + pgvectorscale. Idempotent.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;

-- Review truth
CREATE TABLE IF NOT EXISTS prs (
    id            bigserial PRIMARY KEY,
    repo          text NOT NULL,
    number        int  NOT NULL,
    head_sha      text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repo, number, head_sha)
);

CREATE TABLE IF NOT EXISTS reviews (
    id               bigserial PRIMARY KEY,
    pr_id            bigint NOT NULL REFERENCES prs(id),
    delivery_id      text NOT NULL UNIQUE,          -- X-GitHub-Delivery idempotency key
    status           text NOT NULL DEFAULT 'running', -- running|pending_human|posted|failed
    overall_confidence real,
    github_review_id bigint,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
    id          bigserial PRIMARY KEY,
    review_id   bigint NOT NULL REFERENCES reviews(id),
    agent_type  text NOT NULL CHECK (agent_type IN ('security','quality','tests','docs')),
    severity    text NOT NULL CHECK (severity IN ('CRITICAL','HIGH','MEDIUM','LOW','INFO')),
    category    text NOT NULL,
    file        text NOT NULL,
    line        int,
    confidence  real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rationale   text NOT NULL,
    disputed    boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS hitl_items (
    id          bigserial PRIMARY KEY,
    review_id   bigint NOT NULL REFERENCES reviews(id),
    reason      text NOT NULL,                       -- low_confidence|critical|dispute
    decision    text,                                -- approved|rejected, null while pending
    decided_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);

-- Memory: code chunks (256-dim embeddings), hybrid retrieval = DiskANN + full-text
CREATE TABLE IF NOT EXISTS code_chunks (
    id           bigserial PRIMARY KEY,
    repo         text NOT NULL,
    path         text NOT NULL,
    content      text NOT NULL,
    content_hash text NOT NULL,                      -- skip re-embedding unchanged chunks
    embedding    vector(256) NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repo, path, content_hash)
);
CREATE INDEX IF NOT EXISTS code_chunks_embedding_idx ON code_chunks USING diskann (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS code_chunks_tsv_idx ON code_chunks USING gin (tsv);

-- Time: every span, LLM call, tool call, decision
CREATE TABLE IF NOT EXISTS agent_events (
    time           timestamptz NOT NULL DEFAULT now(),
    review_id      bigint,
    pr_id          bigint,
    agent          text NOT NULL,
    event_type     text NOT NULL,                    -- span|llm.call|tool.call|decision|failure|budget
    cost_usd       numeric(12,6) NOT NULL DEFAULT 0,
    latency_ms     int,
    tokens_in      int,
    tokens_out     int,
    model          text,
    prompt_version text,
    context_ids    bigint[],                         -- code_chunks retrieved for this call
    payload        jsonb NOT NULL DEFAULT '{}'
);
SELECT create_hypertable('agent_events', 'time', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS agent_events_review_idx ON agent_events (review_id, time DESC);

-- Rollups (continuous aggregates)
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', time) AS bucket,
       agent,
       count(*) FILTER (WHERE event_type = 'llm.call') AS calls,
       count(*) FILTER (WHERE event_type = 'failure')  AS failures,
       sum(cost_usd)                                   AS cost_usd,
       percentile_agg(latency_ms)                      AS latency_pct  -- approx_percentile(0.95, latency_pct) = p95
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', time) AS bucket,
       pr_id,
       sum(cost_usd) AS cost_usd,
       count(*) FILTER (WHERE event_type = 'llm.call') AS calls
FROM agent_events
GROUP BY bucket, pr_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agent_health_1m',
    start_offset => INTERVAL '1 hour', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('pr_cost_hourly',
    start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
