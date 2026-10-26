import pytest

from app.orchestrator.agents import MODELS

# Budget and cost tests need priced models. Production defaults are free-tier Gemini (costed at 0),
# so every test runs with the priced Claude names unless it overrides them.
PRICED = {"security": "claude-opus-5-5", "quality": "claude-sonnet-5-5",
          "tests": "claude-sonnet-5-5", "docs": "claude-sonnet-5-5"}


@pytest.fixture(autouse=True)
def priced_models(monkeypatch):
    for agent, model in PRICED.items():
        monkeypatch.setitem(MODELS, agent, model)
