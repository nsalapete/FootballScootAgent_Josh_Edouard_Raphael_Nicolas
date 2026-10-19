"""Player queries. Owner: B.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
"""

from app.database.neo4j import Neo4jClient
from app.models.common import EntityMatch, Page
from app.models.players import PlayerSearch, PlayerSummary, TeammatePath, Valuation


class PlayerService:
    """Read-only queries on `Player`, `PLAYED_WITH` and `Valuation`."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def search(self, criteria: PlayerSearch) -> Page[PlayerSummary]:
        """Players matching the criteria, sorted by market value then id, keyset-paginated."""
        raise NotImplementedError

    def get(self, player_id: int) -> PlayerSummary | None:
        """One player with his current club, or None if the id is unknown."""
        raise NotImplementedError

    def teammate_path(self, from_id: int, to_id: int, max_hops: int = 6) -> TeammatePath | None:
        """Shortest `PLAYED_WITH` chain between two players, None if none within `max_hops` (≤ 6)."""
        raise NotImplementedError

    def value_trend(self, player_id: int) -> list[Valuation]:
        """Market-value history of a player, oldest first."""
        raise NotImplementedError

    def resolve(self, name: str, limit: int = 5) -> list[EntityMatch]:
        """Players whose name matches `name` (full-text index `player_name_ft`), best score first."""
        raise NotImplementedError
