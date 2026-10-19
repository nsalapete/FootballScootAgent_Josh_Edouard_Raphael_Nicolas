"""Rumour endpoints. Owner: D."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.database.neo4j import Neo4jClient, get_db
from app.models.rumours import ImportReport, ImportRequest, RumourOut, RumourStatus
from app.security import require_api_key
from app.services.rumours import RumourService

router = APIRouter(prefix="/api", tags=["rumours"])


def get_rumour_service(db: Neo4jClient = Depends(get_db)) -> RumourService:
    """Build the service for one request."""
    return RumourService(db)


@router.get("/players/{player_id}/rumours", response_model=list[RumourOut])
def player_rumours(
    player_id: int,
    status: Annotated[RumourStatus | None, Query()] = None,
    service: RumourService = Depends(get_rumour_service),
) -> list[RumourOut]:
    """Press rumours about a player, with source, confidence and status."""
    return service.for_player(player_id, status)


@router.post("/rumours/import", response_model=ImportReport, dependencies=[Depends(require_api_key)])
def import_rumours(body: ImportRequest, service: RumourService = Depends(get_rumour_service)) -> ImportReport:
    """Import articles and extract rumours (protected by X-API-Key: it spends LLM credit)."""
    return service.import_articles(body)
