"""Agent models. Owner: D.

Evidence ids use the form `Label:id`, for example `Player:12`, `Transfer:t01`, `Club:4`, `Rumour:r1`,
or `PREDICTED_MOVE:9->5` for a prediction. The verifier checks that each one exists in the graph.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

Nature = Literal["verified", "predicted", "rumour"]


class Claim(BaseModel):
    """One statement of the answer, with its level of truth and the graph ids that prove it."""

    text: str
    nature: Nature
    evidence_ids: list[str] = Field(default_factory=list)
    removal_reason: str | None = Field(None, description="Set by the verifier when the claim is removed")


class ToolCall(BaseModel):
    """One step of the agent trace, in call order."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool = True
    summary: str = ""


class AgentQuestion(BaseModel):
    """Body of `POST /api/agent/ask`."""

    question: str = Field(min_length=3, max_length=500)


class AgentAnswer(BaseModel):
    """Answer of the agent: text, checked claims, removed claims and the full tool trace."""

    answer: str
    claims: list[Claim] = Field(default_factory=list)
    removed_claims: list[Claim] = Field(default_factory=list)
    trace: list[ToolCall] = Field(default_factory=list)
    refused: bool = False
    needs_clarification: bool = Field(False, description="True when the answer is a question about an ambiguous name")
