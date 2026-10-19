"""Player endpoints. Owner: B."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.database.neo4j import Neo4jClient, get_db
from app.models.common import ErrorResponse, Page
from app.models.players import PlayerSearch, PlayerSummary, TeammatePath
from app.services.players import PlayerService

router = APIRouter(prefix="/api/players", tags=["players"])


def get_player_service(db: Neo4jClient = Depends(get_db)) -> PlayerService:
    """Build the service for one request."""
    return PlayerService(db)


@router.get("/search", response_model=Page[PlayerSummary])
def search_players(
    criteria: Annotated[PlayerSearch, Query()],
    service: PlayerService = Depends(get_player_service),
) -> Page[PlayerSummary]:
    """Search players by position, age, value and competition (keyset pagination)."""
    return service.search(criteria)


@router.get("/path", response_model=TeammatePath, responses={404: {"model": ErrorResponse}})
def teammate_path(
    from_id: Annotated[int, Query(alias="from", description="player_id de départ")],
    to_id: Annotated[int, Query(alias="to", description="player_id d'arrivée")],
    max_hops: Annotated[int, Query(ge=1, le=6)] = 6,
    service: PlayerService = Depends(get_player_service),
) -> TeammatePath:
    """Shortest chain of teammates between two players."""
    path = service.teammate_path(from_id, to_id, max_hops)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Aucun chemin de coéquipiers en {max_hops} sauts ou moins")
    return path
