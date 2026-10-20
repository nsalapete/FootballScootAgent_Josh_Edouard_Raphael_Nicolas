"""Rumour models. Owner: D."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

RumourStatus = Literal["pending", "confirmed", "expired", "contradicted"]


class RumourOut(BaseModel):
    """A transfer rumour extracted from the press. Never a fact until its status is `confirmed`."""

    rumour_id: str
    player_id: int
    club_id: int
    club_name: str
    status: RumourStatus
    confidence: float = Field(ge=0, le=1)
    source: str
    url: str
    published_at: date | None = None
    quote: str | None = None
    nature: Literal["rumour"] = "rumour"
    extraction_confidence: float | None = Field(None, ge=0, le=1, description="Confidence given by the extraction")
    source_reliability: float | None = Field(
        None, ge=0, le=1, description="Smoothed share of the source's decided rumours that were confirmed"
    )


class ImportRequest(BaseModel):
    """Body of `POST /api/rumours/import`. Empty `feed_urls` means the default feeds."""

    feed_urls: list[str] = Field(default_factory=list, max_length=20)
    max_articles: int = Field(50, ge=1, le=500)


class ImportReport(BaseModel):
    """Result of an article import."""

    articles_seen: int
    articles_new: int
    rumours_created: int
    unresolved_names: int
    feeds_failed: int = Field(0, description="Feeds that could not be downloaded or parsed")
    extraction_errors: int = Field(0, description="Articles whose LLM extraction failed (retried next time)")
    rejected_quotes: int = Field(0, description="Extracted rumours whose quote is not in the article")
