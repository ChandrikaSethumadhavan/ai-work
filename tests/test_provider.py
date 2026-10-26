import json

import httpx
import pytest

from app.orchestrator.agents import DEFAULT_MODELS, cost, load_models
from app.orchestrator.graph import build_graph, run_review
from app.orchestrator.llm import GOOGLE_OPENAI_BASE, anthropic_llm, make_llm, openai_compat_llm
from app.retrieval.embed import DIMS, embed, fit
from tests.test_orchestrator import finding

KEY = "test-key-123"


def mock(handler):
    return httpx.MockTransport(handler)


def chat_reply(text, usage=None):
    body = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_chat_request_shape_and_parsing():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers["authorization"], body=json.loads(req.content))
        return chat_reply("[]", {"prompt_tokens": 12, "completion_tokens": 3})
    llm = openai_compat_llm("https://example.test/v1/", KEY, transport=mock(handler))
    res = await llm("gemini-2.5-flash", "SYS", "USER", "high")
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}" and KEY not in seen["url"]
    assert seen["body"] == {"model": "gemini-2.5-flash", "messages": [
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]}
    assert (res.text, res.tokens_in, res.tokens_out) == ("[]", 12, 3)


@pytest.mark.asyncio
async def test_missing_usage_and_empty_content_are_tolerated_errors_raise():
    ok = openai_compat_llm("https://x/v1", KEY, transport=mock(lambda r: chat_reply(None)))
    res = await ok("m", "s", "u", "low")
    assert (res.text, res.tokens_in, res.tokens_out) == ("", 0, 0)
    limited = openai_compat_llm("https://x/v1", KEY, transport=mock(lambda r: httpx.Response(429, json={})))
    with pytest.raises(httpx.HTTPStatusError):  # the retry wrapper treats this as retryable
        await limited("m", "s", "u", "low")


@pytest.mark.asyncio
async def test_agents_work_end_to_end_with_fenced_json_replies():
    # Gemini often wraps JSON in a ```json fence; the parser must cope.
    def handler(req):
        system = json.loads(req.content)["messages"][0]["content"]
        agent = system.split("the ")[1].split(" specialist")[0]
        fenced = "```json\n" + json.dumps([finding(agent)]) + "\n```"
        return chat_reply(fenced, {"prompt_tokens": 100, "completion_tokens": 20})
    llm = openai_compat_llm(GOOGLE_OPENAI_BASE, KEY, transport=mock(handler))
    out = await run_review(build_graph(llm), "diff", "ctx", "p1")
    assert not out.get("failures") and sorted(f["agent_type"] for f in out["findings"]) == ["docs", "quality", "security", "tests"]


def test_provider_selection_from_settings():
    assert make_llm({"ANTHROPIC_API_KEY": "a"}) is anthropic_llm
    assert callable(make_llm({"LLM_API_KEY": "k"}))
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        make_llm({})


def test_models_default_to_free_tier_gemini_and_are_overridable():
    assert load_models({}) == DEFAULT_MODELS
    assert all(m.startswith("gemini-") for m in DEFAULT_MODELS.values())
    assert load_models({"REVIEW_MODEL_DOCS": "my-model"})["docs"] == "my-model"


def test_unpriced_models_cost_zero_priced_ones_do_not():
    assert cost("gemini-2.5-flash", 1_000_000, 1_000_000) == 0.0
    assert cost("claude-sonnet-5-5", 1_000_000, 0) == 2.0


def embed_handler(seen):
    def handler(req):
        body = json.loads(req.content)
        seen.append((str(req.url), req.headers.get("x-goog-api-key"), body))
        n = len(body["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [3.0, 4.0] + [0.0] * 3070} for _ in range(n)]})
    return handler


def test_embeddings_request_shape_256_dims_batching_and_header_key():
    seen = []
    vectors = embed([f"t{i}" for i in range(250)], env={"LLM_API_KEY": KEY}, transport=mock(embed_handler(seen)))
    assert [len(s[2]["requests"]) for s in seen] == [100, 100, 50]  # batches of at most 100
    url, key, body = seen[0]
    assert url.endswith("/models/gemini-embedding-001:batchEmbedContents") and key == KEY and KEY not in url
    assert body["requests"][0]["outputDimensionality"] == DIMS and body["requests"][0]["model"] == "models/gemini-embedding-001"
    assert len(vectors) == 250 and all(len(v) == DIMS for v in vectors)


def test_fit_truncates_and_normalizes():
    v = fit([3.0, 4.0] + [0.0] * 3070)  # an API that ignored the requested size
    assert len(v) == DIMS and v[:2] == pytest.approx([0.6, 0.8])
    assert fit([0.0] * 300) == [0.0] * DIMS  # all-zero input does not divide by zero


def test_embedding_count_mismatch_is_an_error():
    bad = mock(lambda r: httpx.Response(200, json={"embeddings": []}))
    with pytest.raises(ValueError):
        embed(["a"], env={"LLM_API_KEY": KEY}, transport=bad)
