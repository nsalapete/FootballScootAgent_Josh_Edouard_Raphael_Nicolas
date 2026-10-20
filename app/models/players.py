"""Player models. Owner: B."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class PlayerSearch(BaseModel):
    """Query parameters of `GET /api/players/search`."""

    position: str | None = Field(None, description="Goalkeeper, Defender, Midfield ou Attack", examples=["Defender"])
    sub_position: str | None = Field(None, description="Ex. Left-Back, Centre-Back", examples=["Left-Back"])
    max_age: int | None = Field(None, ge=15, le=45, examples=[23])
    max_value_eur: int | None = Field(None, ge=0, examples=[15_000_000])
    competition_id: str | None = Field(None, description="Code Transfermarkt, ex. FR1", examples=["FR1"])
    limit: int = Field(20, ge=1, le=100)
    cursor: str | None = Field(None, description="Valeur next_cursor de la page précédente")


class PlayerSummary(BaseModel):
    """A player as listed by search results and agent tools."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "player_id": 5,
                    "name": "Diogo Santos",
                    "position": "Defender",
                    "sub_position": "Left-Back",
                    "date_of_birth": "2004-06-21",
                    "market_value_eur": 9000000,
                    "club_id": 7,
                    "club_name": "SC Alvorada",
                }
            ]
        }
    )

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

    model_config = ConfigDict(json_schema_extra={"examples": [{"as_of": "2024-12-15", "value_eur": 12000000}]})

    as_of: date
    value_eur: int


class PathStep(BaseModel):
    """One player on a teammate path."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"player_id": 1, "name": "Lucas Marchand"}]})

    player_id: int
    name: str


class TeammatePath(BaseModel):
    """Shortest chain of teammates between two players (`PLAYED_WITH`, at most 6 hops)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "players": [
                        {"player_id": 1, "name": "Lucas Marchand"},
                        {"player_id": 6, "name": "Tiago Costa"},
                        {"player_id": 2, "name": "Theo Garnier"},
                        {"player_id": 3, "name": "Joao Ferreira"},
                        {"player_id": 4, "name": "Rui Almeida"},
                    ],
                    "matches_together": [12, 10, 30, 25],
                    "hops": 4,
                }
            ]
        }
    )

    players: list[PathStep]
    matches_together: list[int]
    hops: int
