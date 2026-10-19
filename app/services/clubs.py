"""Club queries. Owner: B.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
"""

from datetime import date

from app.database.neo4j import Neo4jClient
from app.models.clubs import PipelineRow
from app.models.common import EntityMatch


class ClubService:
    """Read-only queries on `Club`, `Transfer` and `Competition`."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def pipeline(self, club_id: int, since: date | None = None, limit: int = 10) -> list[PipelineRow]:
        """Outgoing transfers of a club grouped by destination competition, most frequent first."""
        raise NotImplementedError

    def resolve(self, name: str, limit: int = 5) -> list[EntityMatch]:
        """Clubs whose name matches `name` (full-text index `club_name_ft`), best score first."""
        raise NotImplementedError
