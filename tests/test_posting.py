import os
import uuid

import pytest

from app.aggregate.gate import aggregate
from app.github.post import diff_lines, format_review, post_review, safe
from tests.test_orchestrator import finding

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
+++ not a header, added content
 x = 1
-y = 2
+y = 3
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
"""


def f(line, file="app.py", agent="security", **kw):
    return finding(agent, file=file, line=line, category=f"c{line}", **kw)


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def create_review(self, repo, number, commit_id, body, comments):
        self.calls.append((repo, number, commit_id, body, comments))
        return 4242


def test_diff_lines_counts_added_and_context_not_removed_or_deleted_files():
    assert diff_lines(DIFF) == {"app.py": {1, 2, 3, 4}}  # '+++ not a header' is content on line 2


def test_inline_comment_for_placeable_and_summary_for_the_rest():  # FR-8, decision Q2=A
    review = aggregate([f(4), f(99), f(None, file="other.py")], [])
    body, comments = format_review(review, DIFF)
    assert [(c["path"], c["line"], c["side"]) for c in comments] == [("app.py", 4, "RIGHT")]
    assert "line 99" in body and "`other.py`" in body and "3 finding(s)" in body


def test_incomplete_review_is_flagged_in_summary():
    body, _ = format_review(aggregate([f(1)], [{"agent": "docs", "error": "x"}]), DIFF)
    assert "docs did not finish" in body


def test_mentions_in_model_text_are_defused():  # untrusted text must not ping people
    assert "@​octo" in safe("thanks @octo") and "@" not in safe("a@ b").replace("@ ", "")
    body, comments = format_review(aggregate([f(1, rationale="ping @everyone")], []), DIFF)
    assert "@everyone" not in comments[0]["body"]


needs_db = pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")


@needs_db
def test_live_posts_once_even_when_retried_or_concurrent():  # AC-8, NFR-5
    from concurrent.futures import ThreadPoolExecutor
    from app.aggregate.store import save_review
    from app.db import apply_schema, connect
    conn = connect()
    apply_schema(conn)
    repo = f"test/{uuid.uuid4().hex[:8]}"
    try:
        review = aggregate([f(4)], [])
        assert review["action"] == "post"
        rid = save_review(conn, repo, 7, "sha1", f"d-{repo}", review)
        gh = FakeGitHub()
        args = (rid, repo, 7, "sha1", DIFF, review)
        assert post_review(conn, gh, *args) == 4242
        assert post_review(conn, gh, *args) == 4242  # retry of a finished job
        with ThreadPoolExecutor(4) as pool:  # four workers racing, each with its own connection
            assert set(pool.map(lambda _: post_review(connect(), gh, *args), range(4))) == {4242}
        assert len(gh.calls) == 1
        assert conn.execute("SELECT status, github_review_id FROM reviews WHERE id=%s", (rid,)).fetchone() == ("posted", 4242)
    finally:
        conn.execute("DELETE FROM findings WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM reviews WHERE pr_id IN (SELECT id FROM prs WHERE repo=%s)", (repo,))
        conn.execute("DELETE FROM prs WHERE repo=%s", (repo,))


@needs_db
def test_live_needs_approval_when_gate_says_human():  # NFR-9
    from app.aggregate.store import save_review
    from app.db import connect
    conn = connect()
    repo = f"test/{uuid.uuid4().hex[:8]}"
    try:
        review = aggregate([f(4, severity="CRITICAL")], [])
        rid = save_review(conn, repo, 8, "sha2", f"d-{repo}", review)
        gh = FakeGitHub()
        args = (rid, repo, 8, "sha2", DIFF, review)
        assert post_review(conn, gh, *args) is None and not gh.calls  # escalated: nothing posted
        assert post_review(conn, gh, *args, approved=True) == 4242 and len(gh.calls) == 1  # after a human approves
    finally:
        conn.execute("DELETE FROM hitl_items WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM findings WHERE review_id IN (SELECT r.id FROM reviews r JOIN prs p ON p.id=r.pr_id WHERE p.repo=%s)", (repo,))
        conn.execute("DELETE FROM reviews WHERE pr_id IN (SELECT id FROM prs WHERE repo=%s)", (repo,))
        conn.execute("DELETE FROM prs WHERE repo=%s", (repo,))
