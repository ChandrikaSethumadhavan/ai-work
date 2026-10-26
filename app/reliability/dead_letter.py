import json
import time

from arq import Retry

DEAD_KEY = "review:dead"


def with_dead_letter(job, max_tries: int, base: float = 5.0):
    """Wrap an ARQ job function. ARQ only retries a job that raises `Retry`, so a plain error would fail for good.
    Before the last try: ask ARQ to retry with exponential backoff. On the last try: park the job's arguments in
    a Redis list for a human, then re-raise."""
    async def wrapped(ctx, *args):
        try:
            return await job(ctx, *args)
        except Exception as e:
            attempt = ctx.get("job_try", 1)
            if attempt < max_tries:
                raise Retry(defer=base * 2 ** (attempt - 1)) from e
            await ctx["redis"].lpush(DEAD_KEY, json.dumps(
                {"job": job.__name__, "args": list(args), "error": f"{type(e).__name__}: {e}",
                 "job_id": ctx.get("job_id"), "at": time.time()}))
            raise
    wrapped.__name__, wrapped.__qualname__ = job.__name__, job.__qualname__  # ARQ looks jobs up by qualified name
    return wrapped
