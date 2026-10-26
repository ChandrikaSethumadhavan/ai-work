import test from 'node:test';
import assert from 'node:assert/strict';
import pg from 'pg';
import { loadDashboard } from '../lib/queries.mjs';
import { clientConfig } from '../lib/db.mjs';

const admin = process.env.TIGER_DATABASE_URL;
const readonly = process.env.DASHBOARD_DATABASE_URL;

test('live: seeded review shows up in cost, health, escalation and the queue', { skip: !admin && 'needs TIGER_DATABASE_URL' }, async () => {
  const db = new pg.Client(clientConfig(admin));
  await db.connect();
  const id = Math.random().toString(36).slice(2, 10);
  const repo = `test/${id}`, agent = `zz-${id}`;
  let prId, reviewId;
  try {
    prId = (await db.query('INSERT INTO prs (repo, number, head_sha) VALUES ($1, 1, $2) RETURNING id', [repo, 'sha'])).rows[0].id;
    reviewId = (await db.query(
      "INSERT INTO reviews (pr_id, delivery_id, status, overall_confidence) VALUES ($1, $2, 'pending_human', 0.42) RETURNING id",
      [prId, `d-${id}`])).rows[0].id;
    await db.query("INSERT INTO hitl_items (review_id, reason) VALUES ($1, 'critical')", [reviewId]);
    await db.query(
      `INSERT INTO agent_events (review_id, pr_id, agent, event_type, cost_usd, latency_ms)
       VALUES ($1,$2,$3,'llm.call',0.25,1200), ($1,$2,$3,'llm.call',0.25,800), ($1,$2,$3,'failure',0,NULL)`, [reviewId, prId, agent]);
    await db.query("CALL refresh_continuous_aggregate('agent_health_1m', NULL, NULL)");
    await db.query("CALL refresh_continuous_aggregate('pr_cost_hourly', NULL, NULL)");

    const run = async (url) => {
      const c = new pg.Client(clientConfig(url));
      await c.connect();
      try { return await loadDashboard((sql, p) => c.query(sql, p).then((r) => r.rows)); } finally { await c.end(); }
    };
    for (const url of [admin, readonly].filter(Boolean)) {
      const d = await run(url);
      const cost = d.cost.find((c) => c.repo === repo);
      assert.deepEqual([cost.calls, cost.cost_usd], [2, 0.5]);
      const h = d.health.find((x) => x.agent === agent);
      assert.deepEqual([h.calls, h.failures, h.cost_usd], [2, 1, 0.5]);
      assert.ok(h.p95_ms >= 800 && h.p95_ms <= 1300, `p95 ${h.p95_ms}`);
      assert.ok(d.esc.total >= 1 && d.esc.human >= 1 && d.esc.rate > 0);
      const item = d.queue.find((x) => x.repo === repo);
      assert.deepEqual([item.reason, item.confidence], ['critical', 0.42]);
    }
  } finally {
    await db.query('DELETE FROM agent_events WHERE agent = $1', [agent]);
    if (reviewId) await db.query('DELETE FROM hitl_items WHERE review_id = $1', [reviewId]);
    if (reviewId) await db.query('DELETE FROM reviews WHERE id = $1', [reviewId]);
    if (prId) await db.query('DELETE FROM prs WHERE id = $1', [prId]);
    await db.end();
  }
});

test('live: the read-only role cannot read raw events or write', { skip: !readonly && 'set DASHBOARD_DATABASE_URL after creating the role' }, async () => {
  const c = new pg.Client(clientConfig(readonly));
  await c.connect();
  try {
    await assert.rejects(c.query('SELECT * FROM agent_events LIMIT 1'), /permission denied/);
    await assert.rejects(c.query('SELECT * FROM code_chunks LIMIT 1'), /permission denied/);
    await assert.rejects(c.query("INSERT INTO prs (repo, number, head_sha) VALUES ('x', 1, 'y')"), /permission denied/);
  } finally { await c.end(); }
});
