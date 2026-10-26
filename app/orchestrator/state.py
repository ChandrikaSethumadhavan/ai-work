import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

AGENTS = ("security", "quality", "tests", "docs")


class Finding(BaseModel):
    """The unit that flows through the system (spec FR-5)."""
    agent_type: Literal["security", "quality", "tests", "docs"]
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    category: str
    file: str
    line: int | None = None
    confidence: float = Field(ge=0, le=1)
    rationale: str


class ReviewState(TypedDict, total=False):
    diff: str
    context: str  # retrieved code context (T-03) rendered as text
    context_ids: list[int]  # code_chunks ids retrieved for this review (audit trail)
    muted: list[str]  # categories silenced by repeated disputes (FR-9)
    review: dict  # aggregator output: findings, action, reason, confidence
    findings: Annotated[list[dict], operator.add]
    failures: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    cost_usd: Annotated[float, operator.add]
