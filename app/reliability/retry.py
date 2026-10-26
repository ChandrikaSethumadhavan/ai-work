import asyncio
import time


class CircuitOpen(Exception):
    """The breaker is open: the call was refused without being attempted."""


class CircuitBreaker:
    """Opens after `failures` errors within `window` seconds; refuses calls for `cooldown`, then lets one through."""

    def __init__(self, failures: int = 5, window: float = 60, cooldown: float = 30, clock=time.monotonic):
        self.limit, self.window, self.cooldown, self.clock = failures, window, cooldown, clock
        self.errors: list[float] = []
        self.opened_at: float | None = None

    def before(self):
        if self.opened_at is not None:
            if self.clock() - self.opened_at < self.cooldown:
                raise CircuitOpen("circuit open")
            self.opened_at = None  # half-open: allow a trial call
            self.errors.clear()

    def success(self):
        self.errors.clear()

    def failure(self):
        now = self.clock()
        self.errors = [t for t in self.errors if now - t < self.window] + [now]
        if len(self.errors) >= self.limit:
            self.opened_at = now


async def with_retry(fn, *args, tries: int = 3, base: float = 1.0, breaker: CircuitBreaker | None = None,
                     sleep=asyncio.sleep):
    """Call fn up to `tries` times with exponential backoff (base, 2*base, ...). CircuitOpen is never retried."""
    for attempt in range(tries):
        if breaker:
            breaker.before()
        try:
            result = await fn(*args)
        except Exception:
            if breaker:
                breaker.failure()
            if attempt == tries - 1:
                raise
            await sleep(base * 2 ** attempt)
        else:
            if breaker:
                breaker.success()
            return result


def with_retry_sync(fn, *args, tries: int = 3, base: float = 1.0, breaker: CircuitBreaker | None = None,
                    sleep=time.sleep):
    for attempt in range(tries):
        if breaker:
            breaker.before()
        try:
            result = fn(*args)
        except Exception:
            if breaker:
                breaker.failure()
            if attempt == tries - 1:
                raise
            sleep(base * 2 ** attempt)
        else:
            if breaker:
                breaker.success()
            return result


def resilient_llm(llm, breaker: CircuitBreaker, **kw):
    """Wrap the model call from outside the graph: retries with backoff behind a circuit breaker."""
    async def call(model, system, user, effort):
        return await with_retry(llm, model, system, user, effort, breaker=breaker, **kw)
    return call


class ResilientGitHub:
    """Same interface as GitHubClient.create_review, with retries and its own breaker."""

    def __init__(self, client, breaker: CircuitBreaker | None = None, **kw):
        self.client, self.breaker, self.kw = client, breaker or CircuitBreaker(), kw

    def create_review(self, *args):
        return with_retry_sync(self.client.create_review, *args, breaker=self.breaker, **self.kw)
