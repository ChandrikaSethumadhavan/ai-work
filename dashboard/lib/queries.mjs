// Every query reads rollups or review tables only; `q(sql, params)` returns rows (pg Pool wrapper or a test fake).
const DAYS = 7;

export const costByPr = (q) => q(
  `SELECT p.repo, p.number, round(sum(c.cost_usd)::numeric, 4)::float AS cost_usd, sum(c.calls)::int AS calls
   FROM pr_cost_hourly c JOIN prs p ON p.id = c.pr_id
   WHERE c.bucket > now() - interval '${DAYS} days'
   GROUP BY p.repo, p.number ORDER BY cost_usd DESC LIMIT 20`);

export const agentHealth = (q) => q(
  `SELECT agent, sum(calls)::int AS calls, sum(failures)::int AS failures,
          round(sum(cost_usd)::numeric, 4)::float AS cost_usd,
          approx_percentile(0.95, rollup(latency_pct))::float AS p95_ms
   FROM agent_health_1m WHERE bucket > now() - interval '24 hours'
   GROUP BY agent ORDER BY agent`);

export async function escalation(q) {
  const byStatus = await q(
    `SELECT status, count(*)::int AS n FROM reviews WHERE created_at > now() - interval '${DAYS} days' GROUP BY status`);
  const byReason = await q(
    `SELECT reason, count(*)::int AS n FROM hitl_items WHERE created_at > now() - interval '${DAYS} days'
     GROUP BY reason ORDER BY n DESC`);
  return { ...escalationRate(byStatus), byReason };
}

// Share of reviews that needed a human (not auto-posted).
export function escalationRate(byStatus) {
  const total = byStatus.reduce((s, r) => s + r.n, 0);
  const human = byStatus.filter((r) => r.status === 'pending_human').reduce((s, r) => s + r.n, 0);
  return { total, human, rate: total ? human / total : 0 };
}

export const approvalQueue = (q) => q(
  `SELECT h.id, h.reason, h.created_at, p.repo, p.number, round(r.overall_confidence::numeric, 2)::float AS confidence
   FROM hitl_items h JOIN reviews r ON r.id = h.review_id JOIN prs p ON p.id = r.pr_id
   WHERE h.decision IS NULL ORDER BY h.created_at LIMIT 50`);

export async function loadDashboard(q) {
  const [cost, health, esc, queue] = await Promise.all([costByPr(q), agentHealth(q), escalation(q), approvalQueue(q)]);
  return { cost, health, esc, queue, totalCost: cost.reduce((s, r) => s + r.cost_usd, 0) };
}
