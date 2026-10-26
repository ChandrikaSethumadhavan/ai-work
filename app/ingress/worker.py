import logging
import os

from arq.connections import RedisSettings

log = logging.getLogger(__name__)
MAX_TRIES = 3


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379"))


async def review_pr(ctx, repo: str, pr: int, head_sha: str):
    from app.pipeline import process_pr  # imported here so the web process stays light
    result = await process_pr(ctx["deps"], repo, pr, head_sha, ctx["job_id"])  # job id = GitHub delivery id
    log.info("reviewed %s#%s: %s", repo, pr, result)
    return result


async def startup(ctx):
    from app.pipeline import build_deps, open_saver
    from app.reliability.telemetry import setup_tracing
    setup_tracing()
    ctx["saver_cm"], saver = await open_saver()
    ctx["deps"] = build_deps(saver=saver)


async def shutdown(ctx):
    if ctx.get("saver_cm"):
        await ctx["saver_cm"].__aexit__(None, None, None)


def _functions():
    from app.reliability.dead_letter import with_dead_letter
    return [with_dead_letter(review_pr, max_tries=MAX_TRIES)]


class WorkerSettings:
    functions = _functions()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    max_tries = MAX_TRIES  # retries are requested by the dead-letter wrapper (ARQ retries only on Retry)
    job_timeout = 600  # seconds; ARQ cancels a hung job
