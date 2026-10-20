"""Club queries. Owner: B.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
"""

from datetime import date

from app.database.neo4j import Neo4jClient
from app.models.clubs import PipelineRow
from app.models.common import EntityMatch
from app.services.players import fulltext_query

# Outgoing transfers of a club, aggregated by the competition the destination club played in *that season*
# (a club moves up or down, so its competition is read on `PLAYS_IN {season: t.season}`). A transfer to a club
# outside the followed competitions that season has no row in the subquery and is left out. Loans count as
# transfers; an unknown fee counts as 0 in the sum.
_PIPELINE = """
MATCH (:Club {club_id: $club_id})<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club)
WHERE __SINCE__
CALL (dst, t) {
  MATCH (dst)-[:PLAYS_IN {season: t.season}]->(comp:Competition)
  RETURN comp
  LIMIT 1
}
WITH comp, count(t) AS transfers, sum(coalesce(t.fee_eur, 0)) AS total_fee_eur
RETURN comp.competition_id AS competition_id, comp.name AS competition_name, transfers, total_fee_eur
ORDER BY transfers DESC, total_fee_eur DESC, competition_id ASC
LIMIT $limit
"""
# Two constant variants rather than `$since IS NULL OR t.date >= $since`, which would hide the
# `Transfer(date)` index from the planner. The value of `since` itself always travels in `params`.
_PIPELINE_QUERIES = {
    False: _PIPELINE.replace("__SINCE__", "true"),
    True: _PIPELINE.replace("__SINCE__", "t.date >= $since"),
}

_CLUB_EXISTS = "MATCH (c:Club {club_id: $club_id}) RETURN count(c) > 0 AS found"

_RESOLVE_CLUB = """
CALL db.index.fulltext.queryNodes('club_name_ft', $query) YIELD node, score
RETURN node.club_id AS id, node.name AS name, score
ORDER BY score DESC, id ASC
LIMIT $limit
"""


class ClubService:
    """Read-only queries on `Club`, `Transfer` and `Competition`."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def pipeline(self, club_id: int, since: date | None = None, limit: int = 10) -> list[PipelineRow]:
        """Outgoing transfers of a club grouped by destination competition, most frequent first."""
        query = _PIPELINE_QUERIES[since is not None]
        rows = self.client.read(query, {"club_id": club_id, "since": since, "limit": limit})
        return [PipelineRow(**row) for row in rows]

    def resolve(self, name: str, limit: int = 5) -> list[EntityMatch]:
        """Clubs whose name matches `name` (full-text index `club_name_ft`), best score first."""
        query = fulltext_query(name)
        if not query:
            return []
        rows = self.client.read(_RESOLVE_CLUB, {"query": query, "limit": limit})
        return [EntityMatch(kind="club", **row) for row in rows]

    def exists(self, club_id: int) -> bool:
        """True when a club has this id (lets the API tell an unknown club from a club with no sale)."""
        return self.client.read(_CLUB_EXISTS, {"club_id": club_id})[0]["found"]
