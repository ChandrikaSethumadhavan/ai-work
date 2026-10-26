import re
import time

import httpx
import jwt

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def diff_lines(diff: str) -> dict[str, set[int]]:
    """Per file, the new-side line numbers a review comment can attach to (added and context lines).
    Hunk header counts tell us when a hunk ends, so '+++ x' inside a hunk is content, not a file header."""
    out: dict[str, set[int]] = {}
    path, old, new, cur = None, 0, 0, 0
    for line in diff.splitlines():
        if old <= 0 and new <= 0:
            if line.startswith("+++ "):
                path = line[4:].removeprefix("b/") if line != "+++ /dev/null" else None
                if path:
                    out.setdefault(path, set())
            elif m := re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line):
                old, cur, new = int(m[1] or 1), int(m[2]), int(m[3] or 1)
        elif line[:1] == "+":
            if path:
                out[path].add(cur)
            cur, new = cur + 1, new - 1
        elif line[:1] == "-":
            old -= 1
        elif line[:1] in (" ", ""):
            if path:
                out[path].add(cur)
            cur, old, new = cur + 1, old - 1, new - 1
    return out


def safe(text: str) -> str:
    """Findings come from a model reading untrusted code: defuse @mentions before posting."""
    return re.sub(r"@(?=\w)", "@​", text)


def describe(f: dict) -> str:
    agents = ", ".join(f.get("agents", [f["agent_type"]]))
    return f"**{f['severity']}** [{agents}] {safe(f['category'])} (confidence {f['confidence']:.0%})\n\n{safe(f['rationale'])}"


def format_review(review: dict, diff: str) -> tuple[str, list[dict]]:
    """Returns (summary body, inline comments). Findings GitHub cannot place go in the summary (decision Q2=A)."""
    placeable = diff_lines(diff)
    inline, unplaced = [], []
    for f in review["findings"]:
        if f.get("line") and f["line"] in placeable.get(f["file"], ()):
            inline.append({"path": f["file"], "line": f["line"], "side": "RIGHT", "body": describe(f)})
        else:
            unplaced.append(f)
    body = [f"## Automated review\n\n{len(review['findings'])} finding(s), overall confidence {review['confidence']:.0%}."]
    if review["failures"]:
        body.append("Incomplete: " + ", ".join(x["agent"] for x in review["failures"]) + " did not finish.")
    for f in unplaced:
        where = f"`{f['file']}`" + (f" line {f['line']}" if f.get("line") else "")
        body.append(f"- {where}: {describe(f)}")
    return "\n\n".join(body), inline


class GitHubClient:
    """Posts as a GitHub App installation. Untested against real GitHub until App credentials exist."""

    def __init__(self, app_id: str, private_key: str, installation_id: str):
        self.app_id, self.key, self.installation_id = app_id, private_key, installation_id

    def _token(self) -> str:
        now = int(time.time())
        app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256")
        r = httpx.post(f"{API}/app/installations/{self.installation_id}/access_tokens",
                       headers={**HEADERS, "Authorization": f"Bearer {app_jwt}"}, timeout=30)
        r.raise_for_status()
        return r.json()["token"]

    def create_review(self, repo: str, number: int, commit_id: str, body: str, comments: list[dict]) -> int:
        r = httpx.post(f"{API}/repos/{repo}/pulls/{number}/reviews",
                       headers={**HEADERS, "Authorization": f"Bearer {self._token()}"}, timeout=30,
                       json={"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments})
        r.raise_for_status()
        return r.json()["id"]


def post_review(conn, client, review_id: int, repo: str, number: int, sha: str, diff: str, review: dict,
                approved: bool = False) -> int | None:
    """Post once per review. Returns the GitHub review id, or None when the gate says a human must approve first.
    The row lock makes concurrent workers wait, then see the saved id and skip (NFR-5, AC-8)."""
    if review["action"] != "post" and not approved:
        return None
    with conn.transaction():
        row = conn.execute("SELECT github_review_id FROM reviews WHERE id=%s FOR UPDATE", (review_id,)).fetchone()
        if row[0]:
            return row[0]
        body, comments = format_review(review, diff)
        gh_id = client.create_review(repo, number, sha, body, comments)
        conn.execute("UPDATE reviews SET github_review_id=%s, status='posted' WHERE id=%s", (gh_id, review_id))
        return gh_id
