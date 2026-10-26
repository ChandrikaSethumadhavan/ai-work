import math
import os

import httpx

DIMS = 256
BATCH = 100  # batchEmbedContents accepts at most 100 texts per request
GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta"


def fit(v: list[float]) -> list[float]:
    """Embeddings are trained so a prefix is still a good embedding: keep DIMS values and re-normalize."""
    v = v[:DIMS]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts: list[str], *, env=os.environ, transport=None) -> list[list[float]]:
    """Google gemini-embedding-001 at 256 dims (free tier). Key goes in a header, never the URL."""
    model = env.get("EMBED_MODEL", "gemini-embedding-001")
    base = env.get("EMBED_BASE_URL", GOOGLE_BASE).rstrip("/")
    out: list[list[float]] = []
    with httpx.Client(transport=transport, timeout=60) as http:
        for i in range(0, len(texts), BATCH):
            r = http.post(
                f"{base}/models/{model}:batchEmbedContents", headers={"x-goog-api-key": env["LLM_API_KEY"]},
                json={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                                    "outputDimensionality": DIMS} for t in texts[i:i + BATCH]]})
            r.raise_for_status()
            out += [fit(e["values"]) for e in r.json()["embeddings"]]
    if len(out) != len(texts):
        raise ValueError(f"expected {len(texts)} embeddings, got {len(out)}")
    return out
