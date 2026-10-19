"""Player models. Owner: B."""

from datetime import date

from pydantic import BaseModel, Field


class PlayerSearch(BaseModel):
    """Query parameters of `GET /api/players/search`."""

    position: str | None = Field(None, description="Goalkeeper, Defender, Midfield ou Attack")
    sub_position: str | None = Field(None, description="Ex. Left-Back, Centre-Back")
    max_age: int | None = Field(None, ge=15, le=45)
    max_value_eur: int | None = Field(None, ge=0)
    competition_id: str | None = Field(None, description="Code Transfermarkt, ex. FR1")
    limit: int = Field(20, ge=1, le=100)
    cursor: str | None = Field(None, description="Valeur next_cursor de la page précédente")


class PlayerSummary(BaseModel):
    """A player as listed by search results and agent tools."""

    player_id: int
    name: str
    position: str | None = None
    sub_position: str | None = None
    date_of_birth: date | None = None
    market_value_eur: int | None = None
    club_id: int | None = None
    club_name: str | None = None


class Valuation(BaseModel):
    """One market-value point of a player."""

    as_of: date
    value_eur: int


class PathStep(BaseModel):
    """One player on a teammate path."""

    player_id: int
    name: str


class TeammatePath(BaseModel):
    """Shortest chain of teammates between two players (`PLAYED_WITH`, at most 6 hops)."""

    players: list[PathStep]
    matches_together: list[int]
    hops: int
