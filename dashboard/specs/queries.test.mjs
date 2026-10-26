import test from 'node:test';
import assert from 'node:assert/strict';
import { escalationRate, escalation, loadDashboard } from '../lib/queries.mjs';

test('escalation rate is the share of reviews that went to a human', () => {
  assert.deepEqual(escalationRate([{ status: 'posted', n: 6 }, { status: 'pending_human', n: 2 }]), { total: 8, human: 2, rate: 0.25 });
  assert.deepEqual(escalationRate([]), { total: 0, human: 0, rate: 0 }); // no reviews: no divide by zero
});

test('queries are read-only SELECTs over rollups and review tables only', async () => {
  const seen = [];
  const q = async (sql) => { seen.push(sql); return []; };
  await loadDashboard(q);
  assert.equal(seen.length, 5);
  for (const sql of seen) {
    assert.match(sql.trim(), /^SELECT/i);
    assert.doesNotMatch(sql, /agent_events|code_chunks|findings|INSERT|UPDATE|DELETE/i); // matches the read-only role's grants
  }
});

test('dashboard totals cost and carries the queue through', async () => {
  const q = async (sql) => {
    if (sql.includes('pr_cost_hourly')) return [{ repo: 'o/r', number: 1, cost_usd: 0.5, calls: 4 }, { repo: 'o/r', number: 2, cost_usd: 0.25, calls: 2 }];
    if (sql.includes('FROM reviews WHERE')) return [{ status: 'pending_human', n: 1 }, { status: 'posted', n: 3 }];
    if (sql.includes('hitl_items h')) return [{ id: 9, reason: 'critical', repo: 'o/r', number: 1, confidence: 0.4, created_at: new Date() }];
    return [];
  };
  const d = await loadDashboard(q);
  assert.equal(d.totalCost, 0.75);
  assert.equal(d.esc.rate, 0.25);
  assert.equal(d.queue[0].reason, 'critical');
});
