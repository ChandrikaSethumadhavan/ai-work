import logging
import os

from arq.connections import RedisSettings

log = logging.getLogger(__name__)


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379"))


async def review_pr(ctx, repo: str, pr: int, head_sha: str):
    # ponytail: stub; the LangGraph review (T-04) plugs in here.
    log.info("review_pr %s#%s@%s", repo, pr, head_sha)


class WorkerSettings:
    functions = [review_pr]
    redis_settings = redis_settings()
    max_tries = 3  # retries on failure
    job_timeout = 600  # seconds; ARQ cancels a hung job
