"""Player queries. Owner: B.

Contract: keep these method names, parameters and return types. Agent tools (D) call them directly.
"""

import base64
import binascii
import json
import re
from typing import Any

from app.database.neo4j import Neo4jClient, Record
from app.models.common import EntityMatch, Page
from app.models.players import PathStep, PlayerSearch, PlayerSummary, TeammatePath, Valuation

# Upper bound on the valuation history of one player (a real career has a few dozen points).
MAX_VALUATIONS = 500
# Longest teammate chain searched: six degrees of separation, and the cost of a breadth-first search grows
# roughly as (average number of teammates) ** hops.
MAX_HOPS = 6
# Words shorter than this are matched exactly by `resolve`: one edit on a 3-letter word matches too much.
FUZZY_MIN_LENGTH = 4

# Filters of `search`, one constant Cypher fragment per criterion. Only the fragments of the criteria that
# are set are joined: the planner then sees plain predicates and can seek the (position, date_of_birth)
# and market_value_eur indexes, whereas `$x IS NULL OR ...` forces a full label scan. No user value is ever
# written into the query text: every value travels in `params`.
_SEARCH_FILTERS = {
    "position": "p.position = $position",
    "sub_position": "p.sub_position = $sub_position",
    # Age <= max_age  <=>  the (max_age + 1)th birthday is still to come  <=>  born after today - (max_age + 1) y.
    "max_age": "p.date_of_birth > date() - duration({years: $max_age + 1})",
    "max_value_eur": "p.market_value_eur <= $max_value_eur",
    "competition_id": (
        "EXISTS { MATCH (p)-[:PLAYS_FOR]->(:Club)-[:PLAYS_IN {season: $season}]->"
        "(:Competition {competition_id: $competition_id}) }"
    ),
}
# Keyset condition: strictly after the last row of the previous page in (value DESC, player_id ASC) order.
# Players without a market value sort last (value key -1).
_CURSOR_FILTER = (
    "(coalesce(p.market_value_eur, -1) < $cursor_value"
    " OR (coalesce(p.market_value_eur, -1) = $cursor_value AND p.player_id > $cursor_id))"
)
_SEARCH_MATCH = "MATCH (p:Player) WHERE "
_SEARCH_RETURN = """
WITH p, coalesce(p.market_value_eur, -1) AS value_key
ORDER BY value_key DESC, p.player_id ASC
LIMIT $limit
OPTIONAL MATCH (p)-[:PLAYS_FOR]->(club:Club)
RETURN p.player_id AS player_id, p.name AS name, p.position AS position, p.sub_position AS sub_position,
       p.date_of_birth AS date_of_birth, p.market_value_eur AS market_value_eur,
       club.club_id AS club_id, club.name AS club_name, value_key
ORDER BY value_key DESC, player_id ASC
"""

_LATEST_SEASON = "MATCH ()-[r:PLAYS_IN]->() RETURN max(r.season) AS season"

_GET_PLAYER = """
MATCH (p:Player {player_id: $player_id})
OPTIONAL MATCH (p)-[:PLAYS_FOR]->(club:Club)
RETURN p.player_id AS player_id, p.name AS name, p.position AS position, p.sub_position AS sub_position,
       p.date_of_birth AS date_of_birth, p.market_value_eur AS market_value_eur,
       club.club_id AS club_id, club.name AS club_name
LIMIT 1
"""

_VALUE_TREND = """
MATCH (:Player {player_id: $player_id})-[:HAS_VALUATION]->(v:Valuation)
WHERE v.date IS NOT NULL AND v.value_eur IS NOT NULL
RETURN v.date AS as_of, v.value_eur AS value_eur
ORDER BY as_of ASC
LIMIT $limit
"""

_TEAMMATE_PATH = """
MATCH (start:Player {player_id: $from_id}), (goal:Player {player_id: $to_id})
MATCH path = shortestPath((start)-[:PLAYED_WITH*..__MAX_HOPS__]-(goal))
RETURN [node IN nodes(path) | {player_id: node.player_id, name: node.name}] AS players,
       [rel IN relationships(path) | coalesce(rel.matches, 0)] AS matches_together
"""
# The bound of a variable-length pattern cannot be a Cypher parameter. So there is one constant query per
# allowed bound, built here at import time from integers 1..MAX_HOPS: `teammate_path` picks one of these six
# strings after checking `max_hops`, and nothing coming from a request is ever written into a query.
_TEAMMATE_PATHS = {hops: _TEAMMATE_PATH.replace("__MAX_HOPS__", str(hops)) for hops in range(1, MAX_HOPS + 1)}

_RESOLVE_PLAYER = """
CALL db.index.fulltext.queryNodes('player_name_ft', $query) YIELD node, score
RETURN node.player_id AS id, node.name AS name, score
ORDER BY score DESC, id ASC
LIMIT $limit
"""

_WORD = re.compile(r"\w+")


class InvalidCursorError(ValueError):
    """The `cursor` given to `search` was not produced by `search` (the API answers 422)."""


def encode_cursor(value_key: int, player_id: int) -> str:
    """Opaque cursor holding the sort key of the last row of a page: URL-safe base64 of a small JSON."""
    raw = json.dumps({"v": value_key, "id": player_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[int, int]:
    """Return `(value_key, player_id)` from a cursor, or raise `InvalidCursorError`."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        value_key, player_id = data["v"], data["id"]
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursorError("Curseur de pagination invalide") from exc
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (value_key, player_id)):
        raise InvalidCursorError("Curseur de pagination invalide")
    return value_key, player_id


def fulltext_query(name: str) -> str:
    """Turn free text into a safe Lucene query for the full-text indexes.

    Only word characters are kept, so Lucene operators typed by a user or an LLM (`+ - : " ~ * ( )`...)
    can neither change the query nor raise a syntax error. Words of `FUZZY_MIN_LENGTH` letters or more
    accept one edit (`~1`): a missing accent ("Mbappe") or a typo still matches. Empty if nothing is left.
    """
    terms = _WORD.findall(name.lower())
    return " ".join(f"{term}~1" if len(term) >= FUZZY_MIN_LENGTH else term for term in terms)


def _native(value: Any) -> Any:
    """Convert a Neo4j temporal value (`neo4j.time.Date`) to its Python equivalent; leave others as is."""
    return value.to_native() if hasattr(value, "to_native") else value


def _summary(row: Record) -> PlayerSummary:
    """Build a `PlayerSummary` from a row returned by `_GET_PLAYER` or the search query."""
    return PlayerSummary(**{**row, "date_of_birth": _native(row.get("date_of_birth"))})


class PlayerService:
    """Read-only queries on `Player`, `PLAYED_WITH` and `Valuation`."""

    def __init__(self, db: Neo4jClient):
        self.client = db

    def search(self, criteria: PlayerSearch) -> Page[PlayerSummary]:
        """Players matching the criteria, sorted by market value then id, keyset-paginated."""
        params: dict[str, Any] = {"limit": criteria.limit + 1}
        clauses = []
        for field, fragment in _SEARCH_FILTERS.items():
            value = getattr(criteria, field)
            if value is not None:
                clauses.append(fragment)
                params[field] = value
        if criteria.competition_id is not None:
            params["season"] = self.latest_season()
            if params["season"] is None:
                return Page[PlayerSummary](items=[])
        if criteria.cursor is not None:
            params["cursor_value"], params["cursor_id"] = decode_cursor(criteria.cursor)
            clauses.append(_CURSOR_FILTER)
        query = _SEARCH_MATCH + (" AND ".join(clauses) or "true") + _SEARCH_RETURN
        rows = self.client.read(query, params)
        page, extra = rows[: criteria.limit], rows[criteria.limit :]
        next_cursor = encode_cursor(page[-1]["value_key"], page[-1]["player_id"]) if extra else None
        return Page[PlayerSummary](items=[_summary(row) for row in page], next_cursor=next_cursor)

    def get(self, player_id: int) -> PlayerSummary | None:
        """One player with his current club, or None if the id is unknown."""
        rows = self.client.read(_GET_PLAYER, {"player_id": player_id})
        return _summary(rows[0]) if rows else None

    def teammate_path(self, from_id: int, to_id: int, max_hops: int = 6) -> TeammatePath | None:
        """Shortest `PLAYED_WITH` chain between two players, None if none within `max_hops` (≤ 6)."""
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= MAX_HOPS:
            raise ValueError(f"max_hops doit être un entier de 1 à {MAX_HOPS}")
        if from_id == to_id:  # shortestPath refuses identical ends, which are simply 0 hops apart
            player = self.get(from_id)
            if player is None:
                return None
            return TeammatePath(
                players=[PathStep(player_id=player.player_id, name=player.name)], matches_together=[], hops=0
            )
        rows = self.client.read(_TEAMMATE_PATHS[max_hops], {"from_id": from_id, "to_id": to_id})
        if not rows:
            return None
        steps = [PathStep(**step) for step in rows[0]["players"]]
        return TeammatePath(players=steps, matches_together=rows[0]["matches_together"], hops=len(steps) - 1)

    def value_trend(self, player_id: int) -> list[Valuation]:
        """Market-value history of a player, oldest first."""
        rows = self.client.read(_VALUE_TREND, {"player_id": player_id, "limit": MAX_VALUATIONS})
        return [Valuation(as_of=_native(row["as_of"]), value_eur=row["value_eur"]) for row in rows]

    def resolve(self, name: str, limit: int = 5) -> list[EntityMatch]:
        """Players whose name matches `name` (full-text index `player_name_ft`), best score first."""
        query = fulltext_query(name)
        if not query:
            return []
        rows = self.client.read(_RESOLVE_PLAYER, {"query": query, "limit": limit})
        return [EntityMatch(kind="player", **row) for row in rows]

    def latest_season(self) -> int | None:
        """Most recent season present in the graph (`PLAYS_IN.season`), None on an empty graph."""
        return self.client.read(_LATEST_SEASON)[0]["season"]
