"""Models shared by every router. Owner: B."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

ItemT = TypeVar("ItemT")


class Page(BaseModel, Generic[ItemT]):
    """One page of results. `next_cursor` is None on the last page (keyset pagination)."""

    items: list[ItemT]
    next_cursor: str | None = None


class FieldError(BaseModel):
    """One invalid input of a request, listed in the body of a 422 response."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"location": "query", "field": "max_age", "message": "Input should be less than or equal to 45"}
            ]
        }
    )

    location: str
    field: str
    message: str


class ErrorResponse(BaseModel):
    """Body of every error response. `errors` details each invalid input of a 422, and is absent otherwise."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"detail": "Joueur inconnu : 999999"},
                {
                    "detail": "Requête invalide : max_age (query) : Input should be less than or equal to 45",
                    "errors": [
                        {"location": "query", "field": "max_age", "message": "Input should be less than or equal to 45"}
                    ],
                },
            ]
        }
    )

    detail: str
    errors: list[FieldError] | None = None


class EntityMatch(BaseModel):
    """A name resolved against the full-text indexes `player_name_ft` or `club_name_ft`."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"kind": "player", "id": 8, "name": "Gabriel Souza", "score": 1.38}]}
    )

    kind: Literal["player", "club"]
    id: int
    name: str
    score: float
