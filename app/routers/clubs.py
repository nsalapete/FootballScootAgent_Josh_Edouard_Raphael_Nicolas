"""Club endpoints. Owner: B."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.database.neo4j import Neo4jClient, get_db
from app.models.clubs import PipelineRow
from app.models.common import EntityMatch, ErrorResponse
from app.services.clubs import ClubService

router = APIRouter(prefix="/api/clubs", tags=["clubs"])


def get_club_service(db: Neo4jClient = Depends(get_db)) -> ClubService:
    """Build the service for one request."""
    return ClubService(db)


@router.get("/resolve", response_model=list[EntityMatch])
def resolve_clubs(
    name: Annotated[
        str, Query(min_length=1, max_length=100, description="Nom, même approximatif", examples=["Alvorada"])
    ],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
    service: ClubService = Depends(get_club_service),
) -> list[EntityMatch]:
    """Clubs whose name matches (full-text index, tolerant to a typo or a missing accent)."""
    return service.resolve(name, limit)


@router.get(
    "/{club_id}/pipeline",
    response_model=list[PipelineRow],
    responses={404: {"model": ErrorResponse, "description": "Club inconnu"}},
)
def club_pipeline(
    club_id: Annotated[int, Path(description="club_id Transfermarkt", examples=[7])],
    since: Annotated[
        date | None, Query(description="Transferts à partir de cette date", examples=["2022-01-01"])
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    service: ClubService = Depends(get_club_service),
) -> list[PipelineRow]:
    """Where a club sells its players, by destination competition. Empty list: a known club with no sale."""
    rows = service.pipeline(club_id, since, limit)
    if not rows and not service.exists(club_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Club inconnu : {club_id}")
    return rows
