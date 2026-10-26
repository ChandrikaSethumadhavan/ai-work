import os
from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int


async def anthropic_llm(model: str, system: str, user: str, effort: str) -> LLMResult:
    # Opus/Sonnet 5.5: no sampling params, thinking stays on; effort is the depth control.
    r = await AsyncAnthropic().messages.create(
        model=model, max_tokens=16000, system=system, output_config={"effort": effort},
        messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in r.content if b.type == "text")
    return LLMResult(text, r.usage.input_tokens, r.usage.output_tokens)


GOOGLE_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def openai_compat_llm(base_url: str, api_key: str, transport=None):
    """Chat via any OpenAI-compatible /chat/completions endpoint (Google AI Studio, OpenRouter, ...).
    `effort` is ignored: providers disagree on how to express it, and unknown fields can 400."""
    async def call(model: str, system: str, user: str, effort: str) -> LLMResult:
        async with httpx.AsyncClient(transport=transport, timeout=120) as http:
            r = await http.post(
                f"{base_url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]})
        r.raise_for_status()  # 429/5xx are retried by the resilient wrapper
        data = r.json()
        usage = data.get("usage") or {}
        return LLMResult(data["choices"][0]["message"]["content"] or "",
                         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return call


def make_llm(env=os.environ):
    """Pick the provider from settings. LLM_API_KEY (+ optional LLM_BASE_URL) selects the OpenAI-compatible
    client, Google AI Studio by default; otherwise ANTHROPIC_API_KEY selects the Claude client."""
    if env.get("LLM_API_KEY"):
        return openai_compat_llm(env.get("LLM_BASE_URL", GOOGLE_OPENAI_BASE), env["LLM_API_KEY"])
    if env.get("ANTHROPIC_API_KEY"):
        return anthropic_llm
    raise RuntimeError("Set LLM_API_KEY (Google AI Studio / OpenRouter) or ANTHROPIC_API_KEY")
