"""Load transfermarkt-datasets (DuckDB) into Neo4j. Owner: A.

Run with `python -m scripts.seed_data` (or `make seed`, or the `ingest` Docker service).
Must be idempotent: running it twice gives the same graph (MERGE on unique keys).

Scope (`SCOPE_COMPETITIONS`, `SCOPE_FROM_SEASON`):
- clubs: every club that played a league game of a scope competition from the first scope season on;
- players: those who appeared for a scope club in those games, whose current club is a scope club, or who made
  a scope transfer;
- transfers: from the first scope season on, with a scope club on at least one side. The other side becomes a
  "light" club (id and name only);
- valuations: of scope players, from January 1st of the first scope season on.

Derived relationships are computed at the end:
- `PLAYED_WITH`: aggregated in DuckDB from the appearances of two players for the same club in the same scope game
  (self-join, `a < b`); individual appearances never reach Neo4j;
- `SENDS_TO`: aggregated in Cypher from the `Transfer` nodes, for the clubs passed as a parameter only.

Extraction and filtering run in DuckDB; Neo4j only receives batches of `BATCH_SIZE` rows
(`UNWIND $rows AS row MERGE ...`), always as query parameters.
"""

import hashlib
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from app.config import get_settings
from app.database.neo4j import Neo4jClient
from scripts.country_codes import canonical_name, country_code
from scripts.cypher_files import run_file

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "scripts" / "constraints.cypher"
BATCH_SIZE = 5000
# One batch of 5,000 MERGE can exceed the 10 s API timeout on a cold database: the ingestion gets its own limit.
INGEST_TIMEOUT_S = 600.0
POSITIONS = {"Goalkeeper", "Defender", "Midfield", "Attack"}

Row = dict[str, Any]

# ---------------------------------------------------------------------------------------------------------------
# Pure transformations (unit-tested without any database)
# ---------------------------------------------------------------------------------------------------------------


def transfer_id(player_id: int, day: date, from_club_id: int, to_club_id: int) -> str:
    """Stable id of a transfer: first 16 hex digits of sha1('player|date|from|to')."""
    key = f"{player_id}|{day.isoformat()}|{from_club_id}|{to_club_id}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse_season(label: str | None) -> int | None:
    """Transfermarkt season label to its start year: '22/23' -> 2022, '99/00' -> 1999, '' or None -> None."""
    if not label or "/" not in label:
        return None
    start = label.split("/", 1)[0].strip()
    if not start.isdigit():
        return None
    year = int(start)
    if year >= 100:
        return year
    return year + (1900 if year >= 50 else 2000)


def to_fee(value: Decimal | float | int | None) -> int | None:
    """Transfer fee in whole euros; a missing value (None or NaN) stays None, 0 stays 0 (free transfer)."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return int(round(value))


def to_date(value: date | datetime | None) -> date | None:
    """DuckDB DATE or TIMESTAMP to a plain date (sent to Neo4j as a Cypher `date`)."""
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def to_int(value: Any) -> int | None:
    """Integer or None, for ids stored as text in some DuckDB tables (`clubs.club_id`)."""
    if value is None or value == "":
        return None
    return int(value)


def batches(rows: list[Row], size: int = BATCH_SIZE) -> Iterator[list[Row]]:
    """Consecutive slices of `rows` of at most `size` items."""
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# ---------------------------------------------------------------------------------------------------------------
# Extraction from DuckDB
# ---------------------------------------------------------------------------------------------------------------

SCOPE_GAMES_SQL = """
CREATE OR REPLACE TEMP TABLE scope_games AS
SELECT CAST(game_id AS INTEGER) AS game_id, competition_id, CAST(season AS INTEGER) AS season,
       home_club_id, away_club_id, home_club_name, away_club_name
FROM games
WHERE competition_id IN (SELECT unnest($competitions))
  AND TRY_CAST(season AS INTEGER) >= $from_season
"""

SCOPE_PLAYS_IN_SQL = """
CREATE OR REPLACE TEMP TABLE scope_plays_in AS
SELECT DISTINCT club_id, competition_id, season FROM (
    SELECT home_club_id AS club_id, competition_id, season FROM scope_games
    UNION ALL
    SELECT away_club_id AS club_id, competition_id, season FROM scope_games
)
"""

SCOPE_CLUBS_SQL = """
CREATE OR REPLACE TEMP TABLE scope_clubs AS
SELECT ids.club_id, coalesce(any_value(c.name), any_value(g.club_name)) AS name
FROM (SELECT DISTINCT club_id FROM scope_plays_in) ids
LEFT JOIN clubs c ON c.club_id = CAST(ids.club_id AS VARCHAR)
LEFT JOIN (
    SELECT home_club_id AS club_id, home_club_name AS club_name FROM scope_games
    UNION ALL
    SELECT away_club_id, away_club_name FROM scope_games
) g ON g.club_id = ids.club_id
GROUP BY ids.club_id
"""

TRANSFERS_SQL = """
SELECT t.player_id, t.transfer_date, t.transfer_season, t.from_club_id, t.to_club_id,
       t.from_club_name, t.to_club_name, t.transfer_fee
FROM transfers t
WHERE (t.from_club_id IN (SELECT club_id FROM scope_clubs) OR t.to_club_id IN (SELECT club_id FROM scope_clubs))
  AND t.player_id IN (SELECT player_id FROM players)
  AND t.transfer_date IS NOT NULL
ORDER BY t.player_id, t.transfer_date
"""

TRANSFER_PLAYERS_SQL = "CREATE OR REPLACE TEMP TABLE transfer_players AS SELECT unnest($ids) AS player_id"

SCOPE_PLAYERS_SQL = """
CREATE OR REPLACE TEMP TABLE scope_players AS
SELECT DISTINCT a.player_id FROM appearances a
JOIN scope_games g ON g.game_id = a.game_id
WHERE a.player_club_id IN (SELECT club_id FROM scope_clubs)
UNION
SELECT p.player_id FROM players p
WHERE TRY_CAST(p.current_club_id AS INTEGER) IN (SELECT club_id FROM scope_clubs)
  AND TRY_CAST(p.last_season AS INTEGER) >= $from_season
UNION
SELECT player_id FROM transfer_players
"""

PLAYERS_SQL = """
SELECT p.player_id, p.name, p.position, p.sub_position, p.date_of_birth, p.market_value_in_eur, p.foot,
       p.height_in_cm, TRY_CAST(p.current_club_id AS INTEGER) AS club_id, p.current_club_name,
       p.country_of_citizenship
FROM players p
WHERE p.player_id IN (SELECT player_id FROM scope_players)
ORDER BY p.player_id
"""

VALUATIONS_SQL = """
SELECT v.player_id, v.date, v.market_value_in_eur
FROM player_valuations v
WHERE v.player_id IN (SELECT player_id FROM scope_players)
  AND v.date >= make_date($from_season, 1, 1)
  AND v.market_value_in_eur IS NOT NULL
ORDER BY v.player_id, v.date
"""

SCOPE_APPEARANCES_SQL = """
CREATE OR REPLACE TEMP TABLE scope_appearances AS
SELECT a.game_id, a.player_club_id AS club_id, a.player_id, g.season
FROM appearances a
JOIN scope_games g ON g.game_id = a.game_id
WHERE a.player_club_id IN (SELECT club_id FROM scope_clubs)
"""

# One row per pair of teammates, stored once from the smaller to the larger player id.
PLAYED_WITH_SQL = """
SELECT x.player_id AS a, y.player_id AS b, count(DISTINCT x.game_id) AS matches,
       min(x.season) AS first_season, max(x.season) AS last_season
FROM scope_appearances x
JOIN scope_appearances y ON y.game_id = x.game_id AND y.club_id = x.club_id AND x.player_id < y.player_id
GROUP BY x.player_id, y.player_id
ORDER BY a, b
"""

COMPETITIONS_SQL = """
SELECT competition_id, name, country_name
FROM competitions
WHERE competition_id IN (SELECT unnest($competitions))
ORDER BY competition_id
"""


@dataclass
class Extract:
    """Rows ready for Neo4j, one list per kind of node or relationship."""

    countries: list[Row] = field(default_factory=list)
    competitions: list[Row] = field(default_factory=list)
    clubs: list[Row] = field(default_factory=list)
    light_clubs: list[Row] = field(default_factory=list)
    plays_in: list[Row] = field(default_factory=list)
    players: list[Row] = field(default_factory=list)
    transfers: list[Row] = field(default_factory=list)
    valuations: list[Row] = field(default_factory=list)
    played_with: list[Row] = field(default_factory=list)

    def club_ids(self) -> list[int]:
        """Every club id of the extract, scope and light."""
        return [row["club_id"] for row in self.clubs + self.light_clubs]


def fetch(con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any] | None = None) -> list[Row]:
    """Run `sql` with `params` and return the rows as dictionaries."""
    cursor = con.execute(sql, params or {})
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, values)) for values in cursor.fetchall()]


def competition_name(slug: str | None) -> str | None:
    """Transfermarkt competition slug to a display name: 'premier-league' -> 'Premier League'."""
    return slug.replace("-", " ").title() if slug else None


def transfer_rows(raw: list[Row], from_season: int) -> list[Row]:
    """Shape raw DuckDB transfers, dropping those before `from_season`; club names are kept for light clubs."""
    rows = []
    for item in raw:
        season = parse_season(item["transfer_season"])
        if season is None or season < from_season:
            continue
        day = to_date(item["transfer_date"])
        rows.append(
            {
                "transfer_id": transfer_id(item["player_id"], day, item["from_club_id"], item["to_club_id"]),
                "player_id": item["player_id"],
                "from_club_id": item["from_club_id"],
                "to_club_id": item["to_club_id"],
                "date": day,
                "season": season,
                "fee_eur": to_fee(item["transfer_fee"]),
                "is_loan": False,
                "from_club_name": item["from_club_name"],
                "to_club_name": item["to_club_name"],
            }
        )
    return rows


def player_row(item: Row) -> Row:
    """One DuckDB player as the properties of a `Player` node plus its current club and country."""
    return {
        "player_id": item["player_id"],
        "name": item["name"],
        "position": item["position"] if item["position"] in POSITIONS else None,
        "sub_position": item["sub_position"],
        "date_of_birth": to_date(item["date_of_birth"]),
        "market_value_eur": item["market_value_in_eur"],
        "foot": item["foot"],
        "height_cm": item["height_in_cm"],
        "club_id": item["club_id"],
        "country_code": country_code(item["country_of_citizenship"]),
    }


def light_club_rows(transfers: list[Row], raw_players: list[Row], scope_ids: set[int]) -> list[Row]:
    """Clubs cited by a transfer or as a current club but outside the scope: id and name only."""
    names: dict[int, str | None] = {}
    for item in transfers:
        names.setdefault(item["from_club_id"], item["from_club_name"])
        names.setdefault(item["to_club_id"], item["to_club_name"])
    for item in raw_players:
        if item["club_id"] is not None:
            names.setdefault(item["club_id"], item["current_club_name"])
    return [{"club_id": club, "name": name} for club, name in sorted(names.items()) if club not in scope_ids]


def country_rows(names: list[str | None]) -> list[Row]:
    """One `Country` row per code, named after the canonical spelling."""
    countries: dict[str, str] = {}
    for name in names:
        code = country_code(name)
        if code is not None:
            countries.setdefault(code, canonical_name(name))
    return [{"code": code, "name": name} for code, name in sorted(countries.items())]


def extract(duckdb_path: str | Path, competitions: list[str], from_season: int) -> Extract:
    """Read the scope of the graph from DuckDB (read-only) and shape it into rows for Neo4j."""
    scope = {"competitions": competitions, "from_season": from_season}
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        con.execute(SCOPE_GAMES_SQL, scope)
        con.execute(SCOPE_PLAYS_IN_SQL)
        con.execute(SCOPE_CLUBS_SQL)
        clubs = fetch(con, "SELECT club_id, name FROM scope_clubs ORDER BY club_id")
        plays_in = fetch(con, "SELECT club_id, competition_id, season FROM scope_plays_in ORDER BY ALL")
        transfers = transfer_rows(fetch(con, TRANSFERS_SQL), from_season)
        con.execute(TRANSFER_PLAYERS_SQL, {"ids": sorted({row["player_id"] for row in transfers}) or [0]})
        con.execute(SCOPE_PLAYERS_SQL, {"from_season": from_season})
        con.execute(SCOPE_APPEARANCES_SQL)
        played_with = fetch(con, PLAYED_WITH_SQL)
        raw_players = fetch(con, PLAYERS_SQL)
        raw_valuations = fetch(con, VALUATIONS_SQL, {"from_season": from_season})
        raw_competitions = fetch(con, COMPETITIONS_SQL, {"competitions": competitions})

    competitions_rows = [
        {
            "competition_id": item["competition_id"],
            "name": competition_name(item["name"]),
            "country_code": country_code(item["country_name"]),
        }
        for item in raw_competitions
    ]
    names = [item["country_of_citizenship"] for item in raw_players] + [
        item["country_name"] for item in raw_competitions
    ]
    return Extract(
        countries=country_rows(names),
        competitions=competitions_rows,
        clubs=clubs,
        light_clubs=light_club_rows(transfers, raw_players, {row["club_id"] for row in clubs}),
        plays_in=plays_in,
        players=[player_row(item) for item in raw_players],
        transfers=transfers,
        valuations=[
            {
                "valuation_id": f"{item['player_id']}-{to_date(item['date']).isoformat()}",
                "player_id": item["player_id"],
                "date": to_date(item["date"]),
                "value_eur": item["market_value_in_eur"],
            }
            for item in raw_valuations
        ],
        played_with=played_with,
    )


# ---------------------------------------------------------------------------------------------------------------
# Loading into Neo4j (constant, parameterised Cypher)
# ---------------------------------------------------------------------------------------------------------------

COUNTRIES_CYPHER = """
UNWIND $rows AS row
MERGE (c:Country {code: row.code})
SET c.name = row.name
"""

COMPETITIONS_CYPHER = """
UNWIND $rows AS row
MERGE (comp:Competition {competition_id: row.competition_id})
SET comp.name = row.name
WITH comp, row WHERE row.country_code IS NOT NULL
MATCH (country:Country {code: row.country_code})
MERGE (comp)-[:IN_COUNTRY]->(country)
"""

CLUBS_CYPHER = """
UNWIND $rows AS row
MERGE (c:Club {club_id: row.club_id})
SET c.name = row.name
"""

# A light club never overwrites the name of a club that already exists (a scope club has the better name).
LIGHT_CLUBS_CYPHER = """
UNWIND $rows AS row
MERGE (c:Club {club_id: row.club_id})
ON CREATE SET c.name = row.name
"""

PLAYS_IN_CYPHER = """
UNWIND $rows AS row
MATCH (club:Club {club_id: row.club_id}), (comp:Competition {competition_id: row.competition_id})
MERGE (club)-[:PLAYS_IN {season: row.season}]->(comp)
"""

PLAYERS_CYPHER = """
UNWIND $rows AS row
MERGE (p:Player {player_id: row.player_id})
SET p.name = row.name, p.position = row.position, p.sub_position = row.sub_position,
    p.date_of_birth = row.date_of_birth, p.market_value_eur = row.market_value_eur,
    p.foot = row.foot, p.height_cm = row.height_cm
"""

# Current club: the previous PLAYS_FOR is removed when the player changed club between two runs.
PLAYS_FOR_CYPHER = """
UNWIND $rows AS row
MATCH (p:Player {player_id: row.player_id})
OPTIONAL MATCH (p)-[old:PLAYS_FOR]->(other:Club)
WHERE row.club_id IS NULL OR other.club_id <> row.club_id
DELETE old
WITH DISTINCT p, row WHERE row.club_id IS NOT NULL
MATCH (c:Club {club_id: row.club_id})
MERGE (p)-[:PLAYS_FOR]->(c)
"""

CITIZEN_OF_CYPHER = """
UNWIND $rows AS row
MATCH (p:Player {player_id: row.player_id})
OPTIONAL MATCH (p)-[old:CITIZEN_OF]->(other:Country)
WHERE row.country_code IS NULL OR other.code <> row.country_code
DELETE old
WITH DISTINCT p, row WHERE row.country_code IS NOT NULL
MATCH (c:Country {code: row.country_code})
MERGE (p)-[:CITIZEN_OF]->(c)
"""

TRANSFERS_CYPHER = """
UNWIND $rows AS row
MATCH (p:Player {player_id: row.player_id})
MATCH (src:Club {club_id: row.from_club_id}), (dst:Club {club_id: row.to_club_id})
MERGE (t:Transfer {transfer_id: row.transfer_id})
SET t.date = row.date, t.season = row.season, t.fee_eur = row.fee_eur, t.is_loan = row.is_loan
MERGE (p)-[:MADE]->(t)
MERGE (t)-[:FROM]->(src)
MERGE (t)-[:TO]->(dst)
"""

VALUATIONS_CYPHER = """
UNWIND $rows AS row
MATCH (p:Player {player_id: row.player_id})
MERGE (v:Valuation {valuation_id: row.valuation_id})
SET v.date = row.date, v.value_eur = row.value_eur
MERGE (p)-[:HAS_VALUATION]->(v)
"""

PLAYED_WITH_CYPHER = """
UNWIND $rows AS row
MATCH (a:Player {player_id: row.a}), (b:Player {player_id: row.b})
MERGE (a)-[r:PLAYED_WITH]->(b)
SET r.matches = row.matches, r.first_season = row.first_season, r.last_season = row.last_season
"""

# Outgoing SENDS_TO of the given clubs, rebuilt from scratch so that a removed transfer leaves no stale count.
# sum() skips null fees (unknown amounts) and gives 0 when every fee of the pair is unknown.
SENDS_TO_CYPHER = """
UNWIND $club_ids AS club_id
MATCH (src:Club {club_id: club_id})
OPTIONAL MATCH (src)-[old:SENDS_TO]->()
DELETE old
WITH DISTINCT src
MATCH (src)<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club)
WITH src, dst, count(t) AS transfers, sum(t.fee_eur) AS total
MERGE (src)-[r:SENDS_TO]->(dst)
SET r.count = transfers, r.total_fee_eur = total
RETURN count(r) AS created
"""
SENDS_TO_BATCH = 500

NODE_COUNTS_CYPHER = "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label LIMIT 100"
RELATIONSHIP_COUNTS_CYPHER = "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS count ORDER BY type LIMIT 100"


def load(db: Neo4jClient, cypher: str, rows: list[Row], size: int = BATCH_SIZE) -> int:
    """Send `rows` to `cypher` in batches of `size`, one write transaction per batch; return the row count."""
    for chunk in batches(rows, size):
        db.write(cypher, {"rows": chunk})
    return len(rows)


def load_extract(db: Neo4jClient, data: Extract, size: int = BATCH_SIZE) -> dict[str, int]:
    """Write every part of `data`, nodes before the relationships that need them."""
    steps = [
        ("countries", COUNTRIES_CYPHER, data.countries),
        ("competitions", COMPETITIONS_CYPHER, data.competitions),
        ("clubs", CLUBS_CYPHER, data.clubs),
        ("light_clubs", LIGHT_CLUBS_CYPHER, data.light_clubs),
        ("plays_in", PLAYS_IN_CYPHER, data.plays_in),
        ("players", PLAYERS_CYPHER, data.players),
        ("plays_for", PLAYS_FOR_CYPHER, data.players),
        ("citizen_of", CITIZEN_OF_CYPHER, data.players),
        ("transfers", TRANSFERS_CYPHER, data.transfers),
        ("valuations", VALUATIONS_CYPHER, data.valuations),
    ]
    return {name: load(db, cypher, rows, size) for name, cypher, rows in steps}


def build_sends_to(db: Neo4jClient, club_ids: list[int], size: int = SENDS_TO_BATCH) -> int:
    """Recompute the outgoing `SENDS_TO` of `club_ids` only; return how many relationships they now have."""
    created = 0
    for start in range(0, len(club_ids), size):
        rows = db.write(SENDS_TO_CYPHER, {"club_ids": club_ids[start : start + size]})
        created += rows[0]["created"] if rows else 0
    return created


def load_derived(db: Neo4jClient, data: Extract, size: int = BATCH_SIZE) -> dict[str, int]:
    """Derived relationships, once every fact is in the graph."""
    return {
        "played_with": load(db, PLAYED_WITH_CYPHER, data.played_with, size),
        "sends_to": build_sends_to(db, data.club_ids()),
    }


def graph_summary(db: Neo4jClient) -> dict[str, dict[str, int]]:
    """Number of nodes per label and of relationships per type in the whole database."""
    return {
        "nodes": {row["label"]: row["count"] for row in db.read(NODE_COUNTS_CYPHER)},
        "relationships": {row["type"]: row["count"] for row in db.read(RELATIONSHIP_COUNTS_CYPHER)},
    }


def ingest(
    db: Neo4jClient,
    duckdb_path: str | Path,
    competitions: list[str],
    from_season: int,
    size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """Constraints, extraction, facts, then derived relationships; return the row counts and durations."""
    started = time.perf_counter()
    run_file(db, CONSTRAINTS)
    data = extract(duckdb_path, competitions, from_season)
    extracted = time.perf_counter()
    loaded = load_extract(db, data, size)
    facts_loaded = time.perf_counter()
    loaded.update(load_derived(db, data, size))
    finished = time.perf_counter()
    return {
        "loaded": loaded,
        "extract_s": round(extracted - started, 1),
        "load_s": round(facts_loaded - extracted, 1),
        "derived_s": round(finished - facts_loaded, 1),
        "club_ids": data.club_ids(),
    }


def print_report(report: dict[str, Any], summary: dict[str, dict[str, int]]) -> None:
    """Print the rows sent per step, the durations and the content of the graph."""
    print("Rows sent to Neo4j:")
    for name, count in report["loaded"].items():
        print(f"  {name:<14} {count:>9,}")
    print(f"Extraction {report['extract_s']} s, facts {report['load_s']} s, derived {report['derived_s']} s")
    print("Nodes per label:")
    for label, count in summary["nodes"].items():
        print(f"  {label:<14} {count:>9,}")
    print("Relationships per type:")
    for rel_type, count in summary["relationships"].items():
        print(f"  {rel_type:<14} {count:>9,}")


def main() -> None:
    """Entry point of the ingestion."""
    settings = get_settings()
    if not Path(settings.duckdb_path).exists():
        raise SystemExit(f"{settings.duckdb_path} not found: run `make data` first.")
    db = Neo4jClient(
        settings.neo4j_uri,
        (settings.neo4j_user, settings.neo4j_password),
        settings.neo4j_database,
        INGEST_TIMEOUT_S,
    )
    try:
        if not db.ping():
            raise SystemExit(f"Neo4j unreachable at {settings.neo4j_uri}.")
        report = ingest(db, settings.duckdb_path, settings.competitions, settings.scope_from_season)
        print_report(report, graph_summary(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
