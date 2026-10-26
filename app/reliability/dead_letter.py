import json
import time

DEAD_KEY = "review:dead"


def with_dead_letter(job, max_tries: int):
    """Wrap an ARQ job function: on the final failed try, park the job's arguments in a Redis list, then re-raise.
    Register this in WorkerSettings in place of the bare function (wiring happens when the worker is connected)."""
    async def wrapped(ctx, *args):
        try:
            return await job(ctx, *args)
        except Exception as e:
            if ctx.get("job_try", 1) >= max_tries:
                await ctx["redis"].lpush(DEAD_KEY, json.dumps(
                    {"job": job.__name__, "args": list(args), "error": f"{type(e).__name__}: {e}",
                     "job_id": ctx.get("job_id"), "at": time.time()}))
            raise
    wrapped.__name__ = job.__name__
    return wrapped
