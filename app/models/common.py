"""Models shared by every router. Owner: B."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    """One page of results. `next_cursor` is None on the last page (keyset pagination)."""

    items: list[ItemT]
    next_cursor: str | None = None


class ErrorResponse(BaseModel):
    """Body of every error response."""

    detail: str


class EntityMatch(BaseModel):
    """A name resolved against the full-text indexes `player_name_ft` or `club_name_ft`."""

    kind: Literal["player", "club"]
    id: int
    name: str
    score: float
