"""Graph Data Science and link prediction. Owner: C.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
Algorithms write their scores on the nodes (`pagerank`, `community_id`, `betweenness`, `embedding`) so that
reads stay cheap.

`refresh` builds two in-memory projections, runs the algorithms in `write` mode and always drops the projections,
even when an algorithm fails. Each step is its own transaction; the API runs `refresh` on a client with the longer
`GDS_TIMEOUT_S` limit (see `app/routers/network.py`), since a step on the full graph can exceed the 10 s of reads.
"""

import logging
import time
from uuid import uuid4

from neo4j.exceptions import DriverError, Neo4jError

from app.database.neo4j import Neo4jClient
from app.models.network import ClubScore, Community, GdsRefreshReport, PredictedMove, SimilarPlayer

logger = logging.getLogger(__name__)

GDS_TIMEOUT_S = 600.0  # same limit as nginx on /api/admin/
EMBEDDING_DIMENSION = 64
RANDOM_SEED = 42
MAX_COMMUNITIES = 100
MAX_CLUBS_PER_COMMUNITY = 100

# Club transfer network: SENDS_TO kept directed for PageRank, and projected a second time as undirected
# for Louvain (modularity is defined on undirected graphs). Both copies are weighted by the transfer count.
PROJECT_CLUBS = """
CALL gds.graph.project($graph, 'Club', {
  SENDS_TO: {type: 'SENDS_TO', orientation: 'NATURAL', properties: 'count'},
  SENDS_TO_UNDIRECTED: {type: 'SENDS_TO', orientation: 'UNDIRECTED', properties: 'count'}
})
YIELD graphName, nodeCount, relationshipCount
RETURN graphName, nodeCount, relationshipCount
"""

# Teammate network: PLAYED_WITH is stored once (smaller id first) and means a symmetric relation.
PROJECT_TEAMMATES = """
CALL gds.graph.project($graph, 'Player', {
  PLAYED_WITH: {type: 'PLAYED_WITH', orientation: 'UNDIRECTED', properties: 'matches'}
})
YIELD graphName, nodeCount, relationshipCount
RETURN graphName, nodeCount, relationshipCount
"""

PAGERANK = """
CALL gds.pageRank.write($graph, {
  relationshipTypes: ['SENDS_TO'], relationshipWeightProperty: 'count', writeProperty: 'pagerank'
})
YIELD nodePropertiesWritten
RETURN nodePropertiesWritten
"""

# concurrency 1: Louvain and FastRP are only reproducible on a single thread.
LOUVAIN = """
CALL gds.louvain.write($graph, {
  relationshipTypes: ['SENDS_TO_UNDIRECTED'], relationshipWeightProperty: 'count',
  writeProperty: 'community_id', concurrency: 1
})
YIELD nodePropertiesWritten, communityCount, modularity
RETURN nodePropertiesWritten, communityCount, modularity
"""

FASTRP = """
CALL gds.fastRP.write($graph, {
  embeddingDimension: $dimension, relationshipWeightProperty: 'matches', iterationWeights: [0.0, 1.0, 1.0],
  randomSeed: $seed, concurrency: 1, writeProperty: 'embedding'
})
YIELD nodePropertiesWritten
RETURN nodePropertiesWritten
"""

DROP = "CALL gds.graph.drop($graph, false) YIELD graphName RETURN graphName"

# Unweighted betweenness on the directed transfer network: share of shortest seller -> buyer paths through a club,
# i.e. the clubs that bridge otherwise separate markets (a weight would be read as a distance, not a strength).
BETWEENNESS = """
CALL gds.betweenness.write($graph, {relationshipTypes: ['SENDS_TO'], writeProperty: 'betweenness'})
YIELD nodePropertiesWritten
RETURN nodePropertiesWritten
"""

HUBS = """
MATCH (c:Club)
WHERE c.pagerank IS NOT NULL
RETURN c.club_id AS club_id, c.name AS name, c.pagerank AS score
ORDER BY score DESC, club_id
LIMIT $limit
"""

COMMUNITIES = """
MATCH (c:Club)
WHERE c.community_id IS NOT NULL
WITH c ORDER BY coalesce(c.pagerank, 0.0) DESC, c.club_id
WITH c.community_id AS community_id, collect(c.name) AS clubs
WHERE size(clubs) >= $min_size
RETURN community_id, size(clubs) AS size, clubs[..$max_clubs] AS clubs
ORDER BY size DESC, community_id
LIMIT $limit
"""

# Brute-force cosine over every embedded player: O(players x dimension), a few hundred ms on the full graph.
# A player without teammates has a zero embedding, whose cosine is NaN: such pairs are dropped.
SIMILAR = """
MATCH (p:Player {player_id: $player_id})
WHERE p.embedding IS NOT NULL
MATCH (other:Player)
WHERE other.embedding IS NOT NULL AND other <> p
WITH other, gds.similarity.cosine(p.embedding, other.embedding) AS similarity
WHERE NOT isNaN(similarity)
RETURN other.player_id AS player_id, other.name AS name, similarity
ORDER BY similarity DESC, player_id
LIMIT $k
"""

GATEWAYS = """
MATCH (c:Club)
WHERE c.betweenness IS NOT NULL
RETURN c.club_id AS club_id, c.name AS name, c.betweenness AS score
ORDER BY score DESC, club_id
LIMIT $limit
"""

PREDICTED_MOVES = """
MATCH (p:Player {player_id: $player_id})-[m:PREDICTED_MOVE]->(c:Club)
RETURN p.player_id AS player_id, c.club_id AS club_id, c.name AS club_name, m.prob AS probability,
       m.model_version AS model_version, m.horizon_seasons AS horizon_seasons, m.trained_until AS trained_until
ORDER BY probability DESC, club_id
LIMIT $limit
"""


class GdsService:
    """In-memory projections, graph algorithms and stored predictions."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def refresh(self) -> GdsRefreshReport:
        """Rebuild the projections and rerun every algorithm, writing results back to the graph."""
        start = time.perf_counter()
        suffix = uuid4().hex[:8]  # unique names: two refreshes never fight over the same projection
        clubs, teammates = f"clubs-{suffix}", f"teammates-{suffix}"
        try:
            self.client.write(PROJECT_CLUBS, {"graph": clubs})
            self.client.write(PAGERANK, {"graph": clubs})
            self.client.write(LOUVAIN, {"graph": clubs})
            self.client.write(BETWEENNESS, {"graph": clubs})
            self.client.write(PROJECT_TEAMMATES, {"graph": teammates})
            self.client.write(FASTRP, {"graph": teammates, "dimension": EMBEDDING_DIMENSION, "seed": RANDOM_SEED})
        finally:
            for name in (clubs, teammates):
                self._drop(name)
        return GdsRefreshReport(
            projections=["clubs", "teammates"],
            algorithms=["pageRank", "louvain", "betweenness", "fastRP"],
            duration_s=round(time.perf_counter() - start, 3),
        )

    def _drop(self, name: str) -> None:
        """Drop one projection; a failure is logged, never raised, so that the other drop still runs and the
        caller sees the original error of the algorithm."""
        try:
            self.client.write(DROP, {"graph": name})
        except (Neo4jError, DriverError) as error:  # server error or connection lost
            logger.warning(f"Could not drop GDS projection {name}: {error}")

    def hubs(self, limit: int = 20) -> list[ClubScore]:
        """Clubs sorted by weighted PageRank on `SENDS_TO`."""
        return [ClubScore(**row) for row in self.client.read(HUBS, {"limit": limit})]

    def gateways(self, limit: int = 20) -> list[ClubScore]:
        """Clubs sorted by betweenness on `SENDS_TO`: the bridges between transfer markets (added method)."""
        return [ClubScore(**row) for row in self.client.read(GATEWAYS, {"limit": limit})]

    def communities(self, min_size: int = 2) -> list[Community]:
        """Louvain communities of clubs with at least `min_size` members, largest first."""
        params = {"min_size": min_size, "max_clubs": MAX_CLUBS_PER_COMMUNITY, "limit": MAX_COMMUNITIES}
        return [Community(**row) for row in self.client.read(COMMUNITIES, params)]

    def similar_players(self, player_id: int, k: int = 10) -> list[SimilarPlayer]:
        """The `k` nearest players by FastRP embedding."""
        return [SimilarPlayer(**row) for row in self.client.read(SIMILAR, {"player_id": player_id, "k": k})]

    def predicted_moves(self, player_id: int, limit: int = 5) -> list[PredictedMove]:
        """Stored `PREDICTED_MOVE` relations of a player, highest probability first."""
        rows = self.client.read(PREDICTED_MOVES, {"player_id": player_id, "limit": limit})
        return [PredictedMove(**row) for row in rows]
