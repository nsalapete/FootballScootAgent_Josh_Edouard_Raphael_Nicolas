"""Graph Data Science and link prediction. Owner: C.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
Algorithms write their scores on the nodes (`pagerank`, `community_id`, `embedding`) so that reads stay cheap.
"""

from app.database.neo4j import Neo4jClient
from app.models.network import ClubScore, Community, GdsRefreshReport, PredictedMove, SimilarPlayer


class GdsService:
    """In-memory projections, graph algorithms and stored predictions."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def refresh(self) -> GdsRefreshReport:
        """Rebuild the projections and rerun every algorithm, writing results back to the graph."""
        raise NotImplementedError

    def hubs(self, limit: int = 20) -> list[ClubScore]:
        """Clubs sorted by weighted PageRank on `SENDS_TO`."""
        raise NotImplementedError

    def communities(self, min_size: int = 2) -> list[Community]:
        """Louvain communities of clubs with at least `min_size` members, largest first."""
        raise NotImplementedError

    def similar_players(self, player_id: int, k: int = 10) -> list[SimilarPlayer]:
        """The `k` nearest players by FastRP embedding."""
        raise NotImplementedError

    def predicted_moves(self, player_id: int, limit: int = 5) -> list[PredictedMove]:
        """Stored `PREDICTED_MOVE` relations of a player, highest probability first."""
        raise NotImplementedError
