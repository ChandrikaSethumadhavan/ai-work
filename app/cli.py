"""Human review queue.   python -m app.cli list | show ID | approve ID --by NAME | reject ID --by NAME
ID is the queue item id shown by `list`. Reads TIGER_DATABASE_URL and the GitHub App settings from the environment."""
import argparse
import os
import sys

from app.db import connect
from app.github.post import GitHubClient, post_review
from app.reliability.retry import ResilientGitHub

PENDING = ("SELECT h.id, h.reason, r.id, p.repo, p.number, p.head_sha, r.overall_confidence, r.status"
           " FROM hitl_items h JOIN reviews r ON r.id=h.review_id JOIN prs p ON p.id=r.pr_id")


def snapshot(conn, review_id: int) -> dict:
    row = conn.execute("SELECT payload->'review' FROM agent_events WHERE review_id=%s AND event_type='review.snapshot'"
                       " ORDER BY time DESC LIMIT 1", (review_id,)).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"no saved review for review {review_id}")
    return row[0]


def item(conn, item_id: int):
    row = conn.execute(PENDING + " WHERE h.id=%s", (item_id,)).fetchone()
    if not row:
        raise SystemExit(f"no queue item {item_id}")
    decided = conn.execute("SELECT decision FROM hitl_items WHERE id=%s", (item_id,)).fetchone()[0]
    if decided:
        raise SystemExit(f"item {item_id} is already {decided}")
    return dict(zip(("id", "reason", "review_id", "repo", "number", "sha", "confidence", "status"), row))


def decide(conn, item_id: int, decision: str, by: str):
    conn.execute("UPDATE hitl_items SET decision=%s, decided_by=%s, decided_at=now() WHERE id=%s", (decision, by, item_id))


def run(argv, conn=None, github=None, out=print):
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        if name != "show":
            p.add_argument("--by", required=True, help="who is deciding (recorded)")
    a = ap.parse_args(argv)
    conn = conn or connect()

    if a.cmd == "list":
        rows = conn.execute(PENDING + " WHERE h.decision IS NULL ORDER BY h.created_at").fetchall()
        for i, reason, rid, repo, number, sha, conf, status in rows:
            out(f"{i}\t{repo}#{number}\t{reason}\tconfidence={conf:.2f}" if conf is not None else f"{i}\t{repo}#{number}\t{reason}")
        out(f"{len(rows)} waiting" if rows else "nothing waiting")
    elif a.cmd == "show":
        it = item(conn, a.id)
        review = snapshot(conn, it["review_id"])
        out(f"{it['repo']}#{it['number']}  reason={it['reason']}  action={review['action']}  confidence={review['confidence']:.2f}")
        for f in review["findings"]:
            out(f"  {f['severity']:8} {f['file']}:{f.get('line')}  {f['category']}  [{', '.join(f.get('agents', []))}]  {f['rationale'][:120]}")
        for x in review["failures"]:
            out(f"  incomplete: {x['agent']} ({x['error'][:80]})")
    elif a.cmd == "approve":
        it = item(conn, a.id)
        if it["reason"] == "dispute":  # nothing to post: a developer disputed an earlier finding
            decide(conn, a.id, "acknowledged", a.by)
            out(f"item {a.id}: dispute acknowledged")
            return
        github = github or GitHubClient.from_env(os.environ)
        diff = github.get_pr_diff(it["repo"], it["number"])
        gh_id = post_review(conn, ResilientGitHub(github), it["review_id"], it["repo"], it["number"], it["sha"],
                            diff, snapshot(conn, it["review_id"]), approved=True)
        decide(conn, a.id, "approved", a.by)
        out(f"item {a.id}: approved by {a.by}, posted as GitHub review {gh_id}")
    elif a.cmd == "reject":
        it = item(conn, a.id)
        decide(conn, a.id, "rejected", a.by)
        conn.execute("UPDATE reviews SET status='rejected' WHERE id=%s AND github_review_id IS NULL", (it["review_id"],))
        out(f"item {a.id}: rejected by {a.by}; nothing posted")


if __name__ == "__main__":
    run(sys.argv[1:])
