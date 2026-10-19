"""Player endpoints. Owner: B."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.database.neo4j import Neo4jClient, get_db
from app.models.common import EntityMatch, ErrorResponse, Page
from app.models.players import PlayerSearch, PlayerSummary, TeammatePath, Valuation
from app.services.players import InvalidCursorError, PlayerService

router = APIRouter(prefix="/api/players", tags=["players"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Joueur inconnu"}}
PlayerId = Annotated[int, Path(description="player_id Transfermarkt", examples=[1])]


def get_player_service(db: Neo4jClient = Depends(get_db)) -> PlayerService:
    """Build the service for one request."""
    return PlayerService(db)


def unknown_player(player_id: int) -> HTTPException:
    """404 raised when no player has this id."""
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Joueur inconnu : {player_id}")


@router.get("/search", response_model=Page[PlayerSummary])
def search_players(
    criteria: Annotated[PlayerSearch, Query()],
    service: PlayerService = Depends(get_player_service),
) -> Page[PlayerSummary]:
    """Search players by position, age, value and competition (keyset pagination)."""
    try:
        return service.search(criteria)
    except InvalidCursorError as exc:
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get(
    "/path",
    response_model=TeammatePath,
    responses={404: {"model": ErrorResponse, "description": "Joueur inconnu, ou aucun chemin dans la borne"}},
)
def teammate_path(
    from_id: Annotated[int, Query(alias="from", description="player_id de départ", examples=[1])],
    to_id: Annotated[int, Query(alias="to", description="player_id d'arrivée", examples=[4])],
    max_hops: Annotated[int, Query(ge=1, le=6)] = 6,
    service: PlayerService = Depends(get_player_service),
) -> TeammatePath:
    """Shortest chain of teammates between two players."""
    path = service.teammate_path(from_id, to_id, max_hops)
    if path is None:
        for player_id in (from_id, to_id):
            if service.get(player_id) is None:
                raise unknown_player(player_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Aucun chemin de coéquipiers en {max_hops} sauts ou moins")
    return path


@router.get("/resolve", response_model=list[EntityMatch])
def resolve_players(
    name: Annotated[
        str, Query(min_length=1, max_length=100, description="Nom, même approximatif", examples=["Gabriel"])
    ],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    service: PlayerService = Depends(get_player_service),
) -> list[EntityMatch]:
    """Players whose name matches (full-text index, tolerant to a typo or a missing accent)."""
    return service.resolve(name, limit)


@router.get("/{player_id}", response_model=PlayerSummary, responses=NOT_FOUND)
def get_player(player_id: PlayerId, service: PlayerService = Depends(get_player_service)) -> PlayerSummary:
    """One player and their current club."""
    player = service.get(player_id)
    if player is None:
        raise unknown_player(player_id)
    return player


@router.get("/{player_id}/valuations", response_model=list[Valuation], responses=NOT_FOUND)
def player_valuations(player_id: PlayerId, service: PlayerService = Depends(get_player_service)) -> list[Valuation]:
    """Market-value history of a player, oldest first."""
    trend = service.value_trend(player_id)
    if not trend and service.get(player_id) is None:
        raise unknown_player(player_id)
    return trend
