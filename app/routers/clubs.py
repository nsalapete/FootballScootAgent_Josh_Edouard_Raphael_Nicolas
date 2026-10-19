"""Club endpoints. Owner: B."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.database.neo4j import Neo4jClient, get_db
from app.models.clubs import PipelineRow
from app.services.clubs import ClubService

router = APIRouter(prefix="/api/clubs", tags=["clubs"])


def get_club_service(db: Neo4jClient = Depends(get_db)) -> ClubService:
    """Build the service for one request."""
    return ClubService(db)


@router.get("/{club_id}/pipeline", response_model=list[PipelineRow])
def club_pipeline(
    club_id: int,
    since: Annotated[date | None, Query(description="Transferts à partir de cette date")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    service: ClubService = Depends(get_club_service),
) -> list[PipelineRow]:
    """Where a club sells its players, by destination competition."""
    return service.pipeline(club_id, since, limit)
