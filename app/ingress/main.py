import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI, HTTPException, Request

from app.ingress.worker import redis_settings

REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def create_app(secret: str | None = None, queue=None) -> FastAPI:
    """queue: object with ARQ's `enqueue_job`; created from REDIS_URL when not given."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.secret = secret or os.environ["GITHUB_WEBHOOK_SECRET"]
        app.state.queue = queue or await create_pool(redis_settings())
        yield

    app = FastAPI(lifespan=lifespan)

    @app.post("/webhook")
    async def webhook(request: Request):
        body = await request.body()
        if not verify_signature(request.app.state.secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(401, "invalid signature")
        delivery = request.headers.get("X-GitHub-Delivery")
        if not delivery:
            raise HTTPException(400, "missing delivery id")
        if request.headers.get("X-GitHub-Event") != "pull_request":
            return {"status": "ignored"}
        payload = json.loads(body)
        if payload.get("action") not in REVIEW_ACTIONS:
            return {"status": "ignored"}
        # ARQ drops a job whose _job_id already exists: the delivery UUID is the idempotency key.
        job = await request.app.state.queue.enqueue_job(
            "review_pr",
            payload["repository"]["full_name"],
            payload["pull_request"]["number"],
            payload["pull_request"]["head"]["sha"],
            _job_id=delivery,
        )
        return {"status": "queued" if job else "duplicate"}

    return app
