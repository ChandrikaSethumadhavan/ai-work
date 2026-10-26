import { loadDashboard } from '../lib/queries.mjs';
import { query } from '../lib/db.mjs';

export const dynamic = 'force-dynamic';

const usd = (n) => `$${Number(n).toFixed(4)}`;
const pct = (n) => `${(n * 100).toFixed(0)}%`;

function Table({ head, rows, empty }) {
  if (!rows.length) return <p className="empty">{empty}</p>;
  return (
    <table>
      <thead><tr>{head.map((h) => <th key={h} className={h.startsWith('#') ? 'num' : ''}>{h.replace('#', '')}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className={head[j].startsWith('#') ? 'num' : ''}>{c}</td>)}</tr>)}</tbody>
    </table>
  );
}

export default async function Page() {
  let d;
  try {
    d = await loadDashboard(query);
  } catch (e) {
    return <main><h1>PR Review Dashboard</h1><p className="bad">Could not load data: {e.message}</p></main>;
  }
  return (
    <main>
      <h1>PR Review Dashboard</h1>
      <p className="sub">Last 7 days (agent health: last 24 hours). Read-only.</p>
      <section className="tiles">
        <div className="card tile"><div className="n">{usd(d.totalCost)}</div><div className="l">Cost across PRs</div></div>
        <div className="card tile"><div className="n">{d.esc.total}</div><div className="l">Reviews</div></div>
        <div className="card tile"><div className="n">{pct(d.esc.rate)}</div><div className="l">Escalation rate ({d.esc.human} of {d.esc.total} needed a human)</div></div>
        <div className="card tile"><div className="n">{d.queue.length}</div><div className="l">Waiting for a human</div></div>
      </section>
      <section className="card"><h2>Approval queue</h2>
        <Table head={['PR', 'Reason', '#Confidence', 'Waiting since']} empty="Nothing waiting."
          rows={d.queue.map((q) => [`${q.repo}#${q.number}`, q.reason, q.confidence == null ? '' : pct(q.confidence), new Date(q.created_at).toLocaleString()])} />
      </section>
      <section className="card"><h2>Cost per PR</h2>
        <Table head={['PR', '#Calls', '#Cost']} empty="No cost recorded yet."
          rows={d.cost.map((c) => [`${c.repo}#${c.number}`, c.calls, usd(c.cost_usd)])} />
      </section>
      <section className="card"><h2>Agent health</h2>
        <Table head={['Agent', '#Calls', '#Failures', '#p95 latency', '#Cost']} empty="No agent activity yet."
          rows={d.health.map((h) => [h.agent, h.calls, h.failures, h.p95_ms == null ? '' : `${Math.round(h.p95_ms)} ms`, usd(h.cost_usd)])} />
      </section>
      <section className="card"><h2>Why reviews went to a human</h2>
        <Table head={['Reason', '#Count']} empty="No escalations." rows={d.esc.byReason.map((r) => [r.reason, r.n])} />
      </section>
    </main>
  );
}
