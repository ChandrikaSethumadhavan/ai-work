import hashlib
import os
import re
import uuid

import pytest

from app.retrieval.search import episodic, keywords, rrf, search, upsert_chunks


def fake_embed(texts):
    """Deterministic 256-dim bag-of-words vectors, so similar text is close."""
    out = []
    for t in texts:
        v = [0.0] * 256
        for w in re.findall(r"\w+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1
        n = sum(x * x for x in v) ** 0.5 or 1
        out.append([x / n for x in v])
    return out


def test_rrf_rewards_agreement():  # FR-13
    assert set(rrf([[1, 2, 3], [3, 2, 9]])[:2]) == {2, 3}  # in both lists beats in one
    assert rrf([[5], []]) == [5]


def test_keywords_dedup_and_limit():
    assert keywords("def parse_token(token): parse_token") == ["parse_token", "token"]
    assert len(keywords(" ".join(f"word{i}x" for i in range(50)))) == 20


needs_db = pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud database")


@needs_db
def test_live_hybrid_retrieval_and_incremental_index():  # AC-10
    from app.db import apply_schema, connect
    conn = connect()
    apply_schema(conn)
    repo = f"test/{uuid.uuid4().hex[:8]}"
    chunks = [("auth.py", "def verify_token(token): return hmac_compare(token, secret)"),
              ("db.py", "def insert_row(table, values): cursor.execute(sql, values)"),
              ("docs.md", "Deployment runs on Railway with managed Redis")]
    try:
        assert upsert_chunks(conn, repo, chunks, fake_embed) == 3
        assert upsert_chunks(conn, repo, chunks, fake_embed) == 0  # unchanged: nothing re-embedded
        assert upsert_chunks(conn, repo, chunks[:2] + [("docs.md", "changed docs")], fake_embed) == 1
        hits = search(conn, repo, "fix verify_token to compare the token securely", fake_embed, k=2)
        assert hits and hits[0]["path"] == "auth.py" and len(hits) <= 2
        assert search(conn, "test/none", "anything", fake_embed) == []
        assert episodic(conn, repo, ["auth.py"]) == []
    finally:
        conn.execute("DELETE FROM code_chunks WHERE repo=%s", (repo,))
