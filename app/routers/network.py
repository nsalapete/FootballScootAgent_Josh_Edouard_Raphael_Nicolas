"""GDS and ML endpoints. Owner: C."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.database.neo4j import Neo4jClient, get_db
from app.models.network import ClubScore, Community, GdsRefreshReport, PredictedMove, SimilarPlayer
from app.security import require_api_key
from app.services.gds import GdsService

router = APIRouter(prefix="/api", tags=["network"])


def get_gds_service(db: Neo4jClient = Depends(get_db)) -> GdsService:
    """Build the service for one request."""
    return GdsService(db)


@router.get("/clubs/hubs", response_model=list[ClubScore])
def club_hubs(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    service: GdsService = Depends(get_gds_service),
) -> list[ClubScore]:
    """Transfer-market hubs by weighted PageRank."""
    return service.hubs(limit)


@router.get("/network/communities", response_model=list[Community])
def communities(
    min_size: Annotated[int, Query(ge=1)] = 2,
    service: GdsService = Depends(get_gds_service),
) -> list[Community]:
    """Transfer pipelines found by Louvain."""
    return service.communities(min_size)


@router.get("/players/{player_id}/similar", response_model=list[SimilarPlayer])
def similar_players(
    player_id: int,
    k: Annotated[int, Query(ge=1, le=50)] = 10,
    service: GdsService = Depends(get_gds_service),
) -> list[SimilarPlayer]:
    """Players with the closest FastRP embedding."""
    return service.similar_players(player_id, k)


@router.get("/players/{player_id}/predicted-moves", response_model=list[PredictedMove])
def predicted_moves(
    player_id: int,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    service: GdsService = Depends(get_gds_service),
) -> list[PredictedMove]:
    """Destinations predicted by the link-prediction model (hypotheses)."""
    return service.predicted_moves(player_id, limit)


@router.post("/admin/gds/refresh", response_model=GdsRefreshReport, dependencies=[Depends(require_api_key)])
def refresh_gds(service: GdsService = Depends(get_gds_service)) -> GdsRefreshReport:
    """Recompute projections and algorithms (protected by X-API-Key)."""
    return service.refresh()
