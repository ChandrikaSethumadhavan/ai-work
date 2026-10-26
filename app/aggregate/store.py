DISPUTES_TO_MUTE = 3  # decision Q2=A


def save_review(conn, repo: str, number: int, head_sha: str, delivery_id: str, review: dict) -> int:
    """Persist a review, its findings and any human-queue item. Idempotent on delivery_id (NFR-5)."""
    row = conn.execute("SELECT id FROM reviews WHERE delivery_id=%s", (delivery_id,)).fetchone()
    if row:
        return row[0]
    pr_id = conn.execute(
        "INSERT INTO prs (repo, number, head_sha) VALUES (%s,%s,%s)"
        " ON CONFLICT (repo, number, head_sha) DO UPDATE SET head_sha=EXCLUDED.head_sha RETURNING id",
        (repo, number, head_sha)).fetchone()[0]
    status = "posted" if review["action"] == "post" else "pending_human"
    review_id = conn.execute(
        "INSERT INTO reviews (pr_id, delivery_id, status, overall_confidence) VALUES (%s,%s,%s,%s) RETURNING id",
        (pr_id, delivery_id, status, review["confidence"])).fetchone()[0]
    for f in review["findings"] + review["muted"]:
        conn.execute(
            "INSERT INTO findings (review_id, agent_type, severity, category, file, line, confidence, rationale)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (review_id, f["agent_type"], f["severity"], f["category"], f["file"], f.get("line"),
             f["confidence"], f["rationale"]))
    if review["action"] != "post":
        conn.execute("INSERT INTO hitl_items (review_id, reason) VALUES (%s,%s)", (review_id, review["reason"]))
    return review_id


def dispute(conn, finding_id: int) -> None:
    """A developer disputes a posted finding: record it and route to a human (FR-9)."""
    review_id = conn.execute("UPDATE findings SET disputed=true WHERE id=%s RETURNING review_id",
                             (finding_id,)).fetchone()[0]
    conn.execute("INSERT INTO hitl_items (review_id, reason) VALUES (%s,'dispute')", (review_id,))


def muted_categories(conn, repo: str, threshold: int = DISPUTES_TO_MUTE) -> set[str]:
    """Categories with enough disputes (min evidence) to change behaviour; below that, feedback is ignored."""
    return {r[0].lower() for r in conn.execute(
        "SELECT f.category FROM findings f JOIN reviews r ON r.id=f.review_id JOIN prs p ON p.id=r.pr_id"
        " WHERE p.repo=%s AND f.disputed GROUP BY lower(f.category), f.category HAVING count(*) >= %s",
        (repo, threshold))}
