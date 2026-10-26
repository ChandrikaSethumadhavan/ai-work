import re

MAX_CHARS = 3000  # well under the embedding model's input limit
MAX_FILE_CHARS = 200_000
SKIP_PARTS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/")
SKIP_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                 ".gz", ".woff", ".woff2", ".ttf", ".svg", ".pyc", "package-lock.json")
DEF = re.compile(r"^(?:async def |def |class |function |export |public |private |fn |func |impl |type |interface )")


def indexable(path: str) -> bool:
    p = path.lower()
    return not any(part in p for part in SKIP_PARTS) and not p.endswith(SKIP_SUFFIXES)


def chunk_file(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split at top-level definitions (and at max_chars). Binary or oversized files give no chunks."""
    if "\x00" in text or len(text) > MAX_FILE_CHARS:
        return []
    chunks, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if cur and ((DEF.match(line) and size > 400) or size + len(line) > max_chars):
            chunks.append("".join(cur).strip())
            cur, size = [], 0
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur).strip())
    return [c[: 2 * max_chars] for c in chunks if len(c) >= 20]  # a single huge line is cut, not embedded whole
