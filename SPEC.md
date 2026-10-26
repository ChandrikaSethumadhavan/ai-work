# Product specification — ai-work

> Status: draft. A coding agent must not implement product code until this specification is approved through Genesis.
> Source of truth for architecture: `.genesis/Designing_and_building_a_PR_review_multi_agent_system.pdf`.

## Problem

Pull-request review is slow and uneven. A GitHub `pull_request` webhook should trigger a grounded, structured review: four parallel specialist agents (security, quality, tests, docs) retrieve codebase context, return structured findings with confidence and rationale, an aggregator merges them, and a confidence-weighted human-in-the-loop (HITL) gate decides whether to post automatically or route to a human. Every action is recorded as an event so reviews can be traced, audited and priced.

## Users

- Developers and maintainers of a single GitHub repo (one GitHub App) who open PRs and receive reviews.
- Human reviewers who handle low-confidence and CRITICAL findings and dispute findings.
- The operator who watches cost, latency and agent health.

## Functional requirements

- FR-1: A FastAPI ingress verifies the GitHub HMAC-SHA256 signature (`X-Hub-Signature-256`), checks the idempotency key (`X-GitHub-Delivery`), enqueues the job to Redis via ARQ, and returns 200 OK immediately.
- FR-2: A redelivered webhook (same `X-GitHub-Delivery`) is acknowledged and dropped without creating a second job.
- FR-3: An ARQ worker pulls jobs from Redis, runs the review, and provides retries, timeouts, and job ids/results.
- FR-4: A LangGraph orchestrator runs four specialist agents (security, quality, tests, docs) in parallel via the `Send` API over a typed shared state, with the aggregator running only after all four complete. Graph state is checkpointed to Redis at each node boundary so a crashed worker resumes without repeating completed LLM calls.
- FR-5: Each specialist returns structured Findings with fields `agent_type`, `severity` (CRITICAL..INFO), `category`, `file`, `line`, `confidence`, `rationale`.
- FR-6: An aggregator merges and deduplicates findings across agents and computes an overall confidence.
- FR-7: A HITL gate applies: high confidence with no CRITICAL finding posts automatically; confidence below threshold routes to a human approval queue; any CRITICAL finding escalates to a human regardless of confidence.
- FR-8: Approved results are posted to the PR via the GitHub API as one summary review plus inline comments on files and lines.
- FR-9: A developer dispute of a posted finding is recorded as feedback and routed to the dispute path; feedback influences behavior only after a minimum evidence threshold.
- FR-10: Tiger Cloud (one managed Postgres with TimescaleDB, pgvector, pgvectorscale) holds all durable data: jobs/review truth (PRs, reviews, findings, HITL rows, GitHub ids), `code_chunks` (256-dim embeddings), and `agent_events`.
- FR-11: `agent_events` is a hypertable partitioned by 1 day recording every span, LLM call, tool call, decision, cost and latency, including prompt version and retrieved context ids.
- FR-12: Continuous aggregates `agent_health_1m` (calls, cost, p95 latency per agent per minute) and `pr_cost_hourly` (cost per PR) serve dashboards and budget checks.
- FR-13: Retrieval gives each specialist grounded context: embed the PR diff, run DiskANN ANN search and keyword full-text search (GIN) over `code_chunks`, merge with RRF to top-k, and place the result in the agent prompt. Procedural conventions (ADRs, severity policy) are loaded alongside; past reviews (episodic) are queryable.
- FR-14: The ingress only enqueues relevant `pull_request` events (opened, synchronize, reopened); all other events are acknowledged and ignored.
- FR-15: A Next.js dashboard reads the rollups and event/trace data (cost, agent health, escalation rate, approval queue) and is deployable on Railway.

## Non-functional requirements

- NFR-1: The webhook acknowledges well within GitHub's 15 s limit (target p95 under 1 s) independent of review duration.
- NFR-2: Secrets (GitHub App key, webhook secret, Anthropic key, Tiger/Redis URLs) come from environment variables and are never logged or committed.
- NFR-3: A per-PR LLM cost budget is configurable and enforced; exceeding it halts further agent calls and records the event.
- NFR-4: Invalid signatures are rejected and never enqueued.
- NFR-5: Posting is idempotent: a retried job never posts duplicate reviews or comments.
- NFR-6: Reliability: timeouts on every graph node, retries with backoff, circuit breakers on LLM and GitHub calls, a dead-letter queue, and graceful degradation when one agent fails.
- NFR-7: Every finding is auditable: its retrieved context, prompt version, model output and cost are reconstructable from events.
- NFR-8: Agent and diff content is untrusted input (prompt-injection aware); agents have no write tools beyond structured output.
- NFR-9: Autonomy is never total: CRITICAL findings always require a human.

## Constraints

- Python + FastAPI, ARQ on Redis, LangGraph, Claude API, Tiger Cloud (single store; no separate vector or time-series DB), Next.js dashboard on Railway.
- Single GitHub App / single repository for v1.
- Repo currently holds only a placeholder `app.py`.

## Non-goals

- Multi-tenant / multi-org support.
- Auto-fixing code or pushing commits.
- Temporal as orchestrator (LangGraph chosen, ADR-001).
- Separate vector DB (Qdrant) or separate time-series DB.

## Acceptance criteria

- AC-1: A correctly signed `pull_request` webhook returns 200 and a job appears in Redis; a bad signature is rejected and enqueues nothing; a non-PR event is ignored.
- AC-2: The same delivery id sent twice yields exactly one job.
- AC-3: For a test PR, security, quality, tests and docs agents run concurrently and the aggregator runs only after all four finish.
- AC-4: Killing the worker after some agents finish and restarting resumes from the checkpoint without repeating completed agents' LLM calls.
- AC-5: One agent failing still yields a review from the others and records the failure.
- AC-6: Every Finding carries agent_type, severity, category, file/line, confidence and rationale; duplicates across agents are merged.
- AC-7: High-confidence review without CRITICAL posts automatically; low confidence goes to the human queue; a CRITICAL finding escalates regardless of confidence.
- AC-8: Retrying a completed job does not post duplicate comments.
- AC-9: `agent_events` is a 1-day-partitioned hypertable and `agent_health_1m` and `pr_cost_hourly` return rows after a review.
- AC-10: A similarity-plus-keyword query over `code_chunks` returns RRF-merged top-k context used in agent prompts.
- AC-11: A disputed finding is recorded and routed; feedback is not applied below the evidence threshold.
- AC-12: For any posted finding, its retrieved context, prompt version, and cost can be reconstructed from events.
- AC-13: The dashboard displays per-PR cost, agent health and the approval queue from the rollups.

## Risks

- LLM cost overrun: NFR-3 budget.
- Large diffs exceed context: chunk by file and cap.
- Prompt injection from PR content: NFR-8.
- LangGraph is newer and unproven at scale (ADR-001): acceptable for single repo; checkpoint to Redis.
- Human bottleneck: monitor escalation rate on events.
- Feedback-loop poisoning: minimum evidence threshold (FR-9).

## Open questions

- Confidence threshold value, per-PR budget, retry limits.
- Claude model per agent; embedding model for 256-dim vectors.
- Repo indexing strategy for `code_chunks`.
