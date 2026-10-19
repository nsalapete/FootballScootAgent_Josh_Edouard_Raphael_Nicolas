"""Health endpoint used by Docker healthchecks and container tests. Owner: B."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.database.neo4j import Neo4jClient, get_db

router = APIRouter(prefix="/api", tags=["health"])


class Health(BaseModel):
    """`ok` when Neo4j answers, `degraded` otherwise (the API itself is up either way)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "ok", "neo4j": True}]})

    status: str
    neo4j: bool


@router.get("/health", response_model=Health)
def health(db: Neo4jClient = Depends(get_db)) -> Health:
    """Report whether the API can reach Neo4j."""
    reachable = db.ping()
    return Health(status="ok" if reachable else "degraded", neo4j=reachable)
