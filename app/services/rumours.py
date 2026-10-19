"""Press rumours: import, extraction and reads. Owner: D.

Contract: keep these method names, parameters and return types. Agent tools call `for_player` directly.
"""

from app.database.neo4j import Neo4jClient
from app.models.rumours import ImportReport, ImportRequest, RumourOut


class RumourService:
    """Reads and writes `Article` and `Rumour` nodes."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def for_player(self, player_id: int, status: str | None = None) -> list[RumourOut]:
        """Rumours about a player, newest first, optionally filtered by status."""
        raise NotImplementedError

    def import_articles(self, request: ImportRequest) -> ImportReport:
        """Fetch articles, extract rumours with the small LLM, resolve names and store them as `pending`."""
        raise NotImplementedError
