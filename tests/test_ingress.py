import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.ingress.main import create_app
from app.ingress.worker import WorkerSettings

SECRET = "s3cret"


class FakeQueue:
    """Mimics ARQ: enqueue_job returns None when the _job_id already exists."""

    def __init__(self):
        self.jobs = {}

    async def enqueue_job(self, fn, *args, _job_id):
        if _job_id in self.jobs:
            return None
        self.jobs[_job_id] = (fn, args)
        return object()


def post(client, payload, *, event="pull_request", delivery="d1", secret=SECRET, sig=None):
    body = json.dumps(payload).encode()
    sig = sig if sig is not None else "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": sig, "X-GitHub-Event": event, "X-GitHub-Delivery": delivery})


PR = {"action": "opened", "repository": {"full_name": "o/r"},
      "pull_request": {"number": 42, "head": {"sha": "abc"}}}


def make():
    q = FakeQueue()
    return q, TestClient(create_app(SECRET, q))


def test_signed_pr_event_is_queued_with_200():  # AC-1
    q, c = make()
    with c:
        r = post(c, PR)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert q.jobs["d1"] == ("review_pr", ("o/r", 42, "abc"))


def test_bad_signature_rejected_and_not_queued():  # AC-1, NFR-4
    q, c = make()
    with c:
        assert post(c, PR, secret="wrong").status_code == 401
        assert post(c, PR, sig="").status_code == 401
    assert not q.jobs


def test_non_pr_event_and_other_actions_ignored():  # AC-1, FR-14
    q, c = make()
    with c:
        assert post(c, PR, event="push").json()["status"] == "ignored"
        assert post(c, {**PR, "action": "closed"}, delivery="d2").json()["status"] == "ignored"
    assert not q.jobs


def test_same_delivery_twice_makes_one_job():  # AC-2
    q, c = make()
    with c:
        assert post(c, PR).json()["status"] == "queued"
        r = post(c, PR)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(q.jobs) == 1


def test_worker_has_retries_and_timeout():  # FR-3
    assert WorkerSettings.max_tries > 1 and WorkerSettings.job_timeout > 0
