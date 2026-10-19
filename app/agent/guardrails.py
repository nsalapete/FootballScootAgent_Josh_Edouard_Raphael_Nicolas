"""Validation of LLM-generated Cypher. Owner: D.

The allow-lists below mirror docs/schema.md: a generated query may only touch these labels and types.
"""

from pydantic import BaseModel, Field

ALLOWED_LABELS = frozenset({"Player", "Club", "Competition", "Country", "Transfer", "Valuation", "Article", "Rumour"})
ALLOWED_RELATIONSHIPS = frozenset(
    {
        "MADE",
        "FROM",
        "TO",
        "PLAYS_FOR",
        "HAS_VALUATION",
        "CITIZEN_OF",
        "PLAYS_IN",
        "IN_COUNTRY",
        "PLAYED_WITH",
        "SENDS_TO",
        "REPORTS",
        "ABOUT",
        "TARGET",
        "CONFIRMED_BY",
        "PREDICTED_MOVE",
        "PREDICTED_LINK",
    }
)


class ValidationResult(BaseModel):
    """Outcome of a validation: `valid` is False as soon as one rule is broken; `reasons` lists them all."""

    valid: bool
    query: str
    reasons: list[str] = Field(default_factory=list)


class CypherValidator:
    """Rejects any generated query that writes, leaves the schema, or could run unbounded."""

    def __init__(self, max_hops: int = 6, max_limit: int = 100):
        self.max_hops = max_hops
        self.max_limit = max_limit
        self.allowed_labels = ALLOWED_LABELS
        self.allowed_relationships = ALLOWED_RELATIONSHIPS

    def validate(self, query: str) -> ValidationResult:
        """Check `query` against every rule without running it."""
        raise NotImplementedError
