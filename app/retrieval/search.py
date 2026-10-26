import hashlib
import re
from pathlib import Path

POOL = 20  # candidates per retriever before RRF


def vec(v: list[float]) -> str:
    return "[" + ",".join(map(str, v)) + "]"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def upsert_chunks(conn, repo: str, chunks: list[tuple[str, str]], embed) -> int:
    """chunks = [(path, content)]. Only chunks whose content hash is new are embedded (incremental index)."""
    have = {(p, h) for p, h in conn.execute("SELECT path, content_hash FROM code_chunks WHERE repo=%s", (repo,))}
    new = [(p, c, content_hash(c)) for p, c in chunks if (p, content_hash(c)) not in have]
    if not new:
        return 0
    for (p, c, h), e in zip(new, embed([c for _, c, _ in new])):
        conn.execute(
            "INSERT INTO code_chunks (repo, path, content, content_hash, embedding) VALUES (%s,%s,%s,%s,%s::vector)"
            " ON CONFLICT DO NOTHING", (repo, p, c, h, vec(e)))
    return len(new)


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion: score = sum 1/(k+rank) over each ranked id list."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, id_ in enumerate(ranking, 1):
            score[id_] = score.get(id_, 0) + 1 / (k + rank)
    return sorted(score, key=lambda i: (-score[i], i))


def keywords(text: str, limit: int = 20) -> list[str]:
    seen = dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text))
    return list(seen)[:limit]


def search(conn, repo: str, diff: str, embed, k: int = 8) -> list[dict]:
    """Hybrid retrieval: DiskANN nearest neighbours + full-text, merged with RRF, top-k chunks."""
    ann = [r[0] for r in conn.execute(
        "SELECT id FROM code_chunks WHERE repo=%s ORDER BY embedding <=> %s::vector LIMIT %s",
        (repo, vec(embed([diff])[0]), POOL))]
    words = keywords(diff)
    fts = [r[0] for r in conn.execute(
        "SELECT id FROM code_chunks WHERE repo=%s AND tsv @@ to_tsquery('simple', %s)"
        " ORDER BY ts_rank(tsv, to_tsquery('simple', %s)) DESC LIMIT %s",
        (repo, " | ".join(words), " | ".join(words), POOL))] if words else []
    top = rrf([ann, fts])[:k]
    if not top:
        return []
    rows = {r[0]: {"id": r[0], "path": r[1], "content": r[2]} for r in conn.execute(
        "SELECT id, path, content FROM code_chunks WHERE id = ANY(%s)", (top,))}
    return [rows[i] for i in top]


def episodic(conn, repo: str, paths: list[str], limit: int = 10) -> list[dict]:
    """Past findings on the same files (what was flagged or disputed before)."""
    cols = ("agent_type", "severity", "category", "file", "line", "rationale", "disputed")
    return [dict(zip(cols, r)) for r in conn.execute(
        "SELECT f.agent_type, f.severity, f.category, f.file, f.line, f.rationale, f.disputed"
        " FROM findings f JOIN reviews r ON r.id=f.review_id JOIN prs p ON p.id=r.pr_id"
        " WHERE p.repo=%s AND f.file = ANY(%s) ORDER BY r.created_at DESC LIMIT %s", (repo, paths, limit))]


def procedural(path: str = "CONVENTIONS.md") -> str:
    """Team conventions / severity policy: small, always loaded."""
    p = Path(path)
    return p.read_text() if p.exists() else ""
