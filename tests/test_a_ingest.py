"""Tests of the ingestion (scripts/seed_data.py). Owner: A.

Unit tests need no database. Integration tests build a tiny DuckDB file shaped like transfermarkt-datasets,
with negative ids and `test-a-` codes only, load it into the test Neo4j and delete everything they created.
"""

from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest

from scripts import seed_data
from scripts.country_codes import CODES, country_code

COMPETITION = "test-a-league"
FROM_SEASON = 2020

# ---------------------------------------------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------------------------------------------


def test_transfer_id_is_stable_and_short():
    first = seed_data.transfer_id(12, date(2022, 7, 1), 7, 4)
    assert first == seed_data.transfer_id(12, date(2022, 7, 1), 7, 4)
    assert len(first) == 16
    assert int(first, 16) >= 0


def test_transfer_id_depends_on_every_part():
    base = seed_data.transfer_id(12, date(2022, 7, 1), 7, 4)
    assert base != seed_data.transfer_id(13, date(2022, 7, 1), 7, 4)
    assert base != seed_data.transfer_id(12, date(2022, 7, 2), 7, 4)
    assert base != seed_data.transfer_id(12, date(2022, 7, 1), 4, 7)


@pytest.mark.parametrize(
    ("label", "expected"),
    [("22/23", 2022), ("99/00", 1999), ("00/01", 2000), ("49/50", 2049), ("2015/2016", 2015), ("", None), (None, None)],
)
def test_parse_season(label, expected):
    assert seed_data.parse_season(label) == expected


def test_parse_season_rejects_garbage():
    assert seed_data.parse_season("unknown") is None
    assert seed_data.parse_season("ab/cd") is None


def test_to_fee():
    assert seed_data.to_fee(Decimal("2300000.000")) == 2300000
    assert seed_data.to_fee(Decimal("0.000")) == 0
    assert seed_data.to_fee(None) is None
    assert seed_data.to_fee(float("nan")) is None
    assert seed_data.to_fee(1500.6) == 1501


def test_to_date_and_to_int():
    assert seed_data.to_date(datetime(2001, 9, 3, 0, 0)) == date(2001, 9, 3)
    assert seed_data.to_date(date(2001, 9, 3)) == date(2001, 9, 3)
    assert seed_data.to_date(None) is None
    assert seed_data.to_int("-7") == -7
    assert seed_data.to_int("") is None


def test_batches_cover_every_row_once():
    rows = [{"i": i} for i in range(12)]
    chunks = list(seed_data.batches(rows, 5))
    assert [len(chunk) for chunk in chunks] == [5, 5, 2]
    assert [row for chunk in chunks for row in chunk] == rows


def test_country_codes_are_unique_trigrams():
    assert len(set(CODES.values())) == len(CODES)
    assert all(len(code) == 3 and code.isupper() for code in CODES.values())


def test_country_code_matches_the_mini_graph_and_folds_aliases():
    assert [country_code(name) for name in ("France", "England", "Portugal", "Brazil")] == ["FRA", "ENG", "POR", "BRA"]
    assert country_code("Türkiye") == country_code("Turkey") == "TUR"
    assert country_code("test-a-land") == "test-a-land"
    assert country_code(None) is None


def test_country_rows_one_per_code():
    rows = seed_data.country_rows(["Turkey", "Türkiye", None, "France"])
    assert rows == [{"code": "FRA", "name": "France"}, {"code": "TUR", "name": "Turkey"}]


def test_competition_name():
    assert seed_data.competition_name("premier-league") == "Premier League"
    assert seed_data.competition_name(None) is None


def test_transfer_rows_filter_seasons_and_keep_unknown_fees():
    raw = [
        _raw_transfer(1, date(2019, 7, 1), "19/20", Decimal("10.000")),
        _raw_transfer(2, date(2020, 7, 1), "20/21", None),
        _raw_transfer(3, date(2020, 8, 1), None, Decimal("5.000")),
    ]
    rows = seed_data.transfer_rows(raw, FROM_SEASON)
    assert [row["player_id"] for row in rows] == [2]
    assert rows[0]["season"] == 2020
    assert rows[0]["fee_eur"] is None
    assert rows[0]["is_loan"] is False
    assert rows[0]["transfer_id"] == seed_data.transfer_id(2, date(2020, 7, 1), 10, 20)


def test_player_row_drops_missing_position():
    item = {
        "player_id": 5,
        "name": "Someone",
        "position": "Missing",
        "sub_position": None,
        "date_of_birth": datetime(2004, 6, 21),
        "market_value_in_eur": None,
        "foot": "left",
        "height_in_cm": 180,
        "club_id": 7,
        "current_club_name": "Club",
        "country_of_citizenship": "Portugal",
    }
    row = seed_data.player_row(item)
    assert row["position"] is None
    assert row["date_of_birth"] == date(2004, 6, 21)
    assert row["country_code"] == "POR"


def test_light_clubs_exclude_scope_clubs():
    transfers = [{"from_club_id": 1, "from_club_name": "A", "to_club_id": 9, "to_club_name": "Far"}]
    players = [{"club_id": 8, "current_club_name": "Elsewhere"}, {"club_id": None, "current_club_name": None}]
    assert seed_data.light_club_rows(transfers, players, {1}) == [
        {"club_id": 8, "name": "Elsewhere"},
        {"club_id": 9, "name": "Far"},
    ]


def _raw_transfer(player_id, day, season, fee):
    return {
        "player_id": player_id,
        "transfer_date": day,
        "transfer_season": season,
        "from_club_id": 10,
        "to_club_id": 20,
        "from_club_name": "Ten",
        "to_club_name": "Twenty",
        "transfer_fee": fee,
    }


# ---------------------------------------------------------------------------------------------------------------
# Integration: tiny DuckDB file -> test Neo4j
# ---------------------------------------------------------------------------------------------------------------

DUCKDB_TABLES = """
CREATE TABLE competitions (competition_id VARCHAR, name VARCHAR, country_name VARCHAR);
CREATE TABLE clubs (club_id VARCHAR, name VARCHAR);
CREATE TABLE games (game_id VARCHAR, competition_id VARCHAR, season VARCHAR, home_club_id INTEGER,
                    away_club_id INTEGER, home_club_name VARCHAR, away_club_name VARCHAR);
CREATE TABLE appearances (game_id INTEGER, player_id INTEGER, player_club_id INTEGER);
CREATE TABLE players (player_id INTEGER, name VARCHAR, position VARCHAR, sub_position VARCHAR,
                      date_of_birth TIMESTAMP, market_value_in_eur INTEGER, foot VARCHAR, height_in_cm INTEGER,
                      current_club_id VARCHAR, current_club_name VARCHAR, country_of_citizenship VARCHAR,
                      last_season VARCHAR);
CREATE TABLE transfers (player_id INTEGER, transfer_date DATE, transfer_season VARCHAR, from_club_id INTEGER,
                        to_club_id INTEGER, from_club_name VARCHAR, to_club_name VARCHAR,
                        transfer_fee DECIMAL(18, 3));
CREATE TABLE player_valuations (player_id INTEGER, date DATE, market_value_in_eur INTEGER);

INSERT INTO competitions VALUES ('test-a-league', 'test-a-league', 'test-a-land');
INSERT INTO clubs VALUES ('-1', 'Test A One FC'), ('-2', 'Test A Two FC');
INSERT INTO games VALUES
    ('-10', 'test-a-league', '2020', -1, -2, 'One', 'Two'),
    ('-11', 'test-a-league', '2021', -2, -1, 'Two', 'One'),
    ('-12', 'test-a-league', '2019', -4, -1, 'Four', 'One');
INSERT INTO appearances VALUES (-10, -1, -1), (-10, -2, -2), (-12, -4, -4);
INSERT INTO players VALUES
    (-1, 'Test A Defender', 'Defender', 'Left-Back', '2003-04-12', 12000000, 'left', 181, '-1', 'One',
     'test-a-land', '2024'),
    (-2, 'Test A Unknown', 'Missing', NULL, '2001-01-01', NULL, NULL, NULL, '-2', 'Two', 'test-a-other', '2024'),
    (-3, 'Test A Mover', 'Attack', 'Centre-Forward', '1999-05-05', 3000000, 'right', 175, '-3', 'Far Away',
     'test-a-land', '2024'),
    (-4, 'Test A Old', 'Midfield', NULL, '1990-01-01', NULL, NULL, NULL, '-4', 'Four', 'test-a-land', '2018');
INSERT INTO transfers VALUES
    (-3, '2020-08-01', '20/21', -3, -1, 'Far Away', 'One', 1500000),
    (-1, '2021-07-01', '21/22', -1, -2, 'One', 'Two', NULL),
    (-2, '2021-01-15', '20/21', -2, -3, 'Two', 'Far Away', 0),
    (-4, '2018-07-01', '18/19', -4, -1, 'Four', 'One', 100);
INSERT INTO player_valuations VALUES (-1, '2019-06-01', 1000000), (-1, '2021-06-30', 5000000);
"""

CLEANUP = [
    "MATCH (p:Player)-[:MADE|HAS_VALUATION]->(x) WHERE p.player_id < 0 DETACH DELETE x",
    "MATCH (n) WHERE (n:Player AND n.player_id < 0) OR (n:Club AND n.club_id < 0) "
    "OR (n:Competition AND n.competition_id STARTS WITH 'test-a-') "
    "OR (n:Country AND n.code STARTS WITH 'test-a-') DETACH DELETE n",
]

TEST_GRAPH_COUNTS = """
CALL () {
  MATCH (p:Player) WHERE p.player_id < 0 RETURN 'Player' AS kind, count(p) AS n
  UNION ALL MATCH (c:Club) WHERE c.club_id < 0 RETURN 'Club' AS kind, count(c) AS n
  UNION ALL MATCH (p:Player)-[:MADE]->(t:Transfer) WHERE p.player_id < 0 RETURN 'Transfer' AS kind, count(t) AS n
  UNION ALL MATCH (p:Player)-[:HAS_VALUATION]->(v) WHERE p.player_id < 0 RETURN 'Valuation' AS kind, count(v) AS n
  UNION ALL MATCH (c:Club)-[r:PLAYS_IN]->() WHERE c.club_id < 0 RETURN 'PLAYS_IN' AS kind, count(r) AS n
  UNION ALL MATCH (p:Player)-[r:PLAYS_FOR]->() WHERE p.player_id < 0 RETURN 'PLAYS_FOR' AS kind, count(r) AS n
  UNION ALL MATCH (p:Player)-[r:CITIZEN_OF]->() WHERE p.player_id < 0 RETURN 'CITIZEN_OF' AS kind, count(r) AS n
  UNION ALL MATCH (t:Transfer)-[r:FROM|TO]->(c:Club) WHERE c.club_id < 0 RETURN 'FROM_TO' AS kind, count(r) AS n
  UNION ALL MATCH (c:Country) WHERE c.code STARTS WITH 'test-a-' RETURN 'Country' AS kind, count(c) AS n
  UNION ALL MATCH (c:Competition) WHERE c.competition_id STARTS WITH 'test-a-' RETURN 'Competition' AS kind,
                                                                                      count(c) AS n
}
RETURN kind, n
"""


def _cleanup(db):
    for query in CLEANUP:
        db.write(query)


def _counts(db):
    return {row["kind"]: row["n"] for row in db.read(TEST_GRAPH_COUNTS)}


@pytest.fixture()
def tiny_duckdb(tmp_path):
    """A transfermarkt-like DuckDB file with negative ids only."""
    path = tmp_path / "tiny.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(DUCKDB_TABLES)
    return path


@pytest.fixture()
def ingested(neo4j_db, tiny_duckdb):
    """The tiny DuckDB file loaded once into the test Neo4j; removed afterwards."""
    _cleanup(neo4j_db)
    report = seed_data.ingest(neo4j_db, tiny_duckdb, [COMPETITION], FROM_SEASON)
    yield report
    _cleanup(neo4j_db)


def test_extract_keeps_only_the_scope(tiny_duckdb):
    data = seed_data.extract(tiny_duckdb, [COMPETITION], FROM_SEASON)
    assert [row["club_id"] for row in data.clubs] == [-2, -1]
    assert [row["club_id"] for row in data.light_clubs] == [-3]
    assert sorted(row["player_id"] for row in data.players) == [-3, -2, -1]
    assert len(data.transfers) == 3
    assert [row["valuation_id"] for row in data.valuations] == ["-1-2021-06-30"]
    assert sorted((row["club_id"], row["season"]) for row in data.plays_in) == [
        (-2, 2020),
        (-2, 2021),
        (-1, 2020),
        (-1, 2021),
    ]


@pytest.mark.neo4j
def test_ingest_builds_the_expected_graph(neo4j_db, ingested):
    assert ingested["loaded"]["transfers"] == 3
    assert _counts(neo4j_db) == {
        "Player": 3,
        "Club": 3,
        "Transfer": 3,
        "Valuation": 1,
        "PLAYS_IN": 4,
        "PLAYS_FOR": 3,
        "CITIZEN_OF": 3,
        "FROM_TO": 6,
        "Country": 2,
        "Competition": 1,
    }


@pytest.mark.neo4j
def test_ingest_properties_follow_the_schema(neo4j_db, ingested):
    assert set(ingested["club_ids"]) == {-1, -2, -3}
    player = neo4j_db.read("MATCH (p:Player {player_id: -1}) RETURN p")[0]["p"]
    assert player["name"] == "Test A Defender"
    assert player["position"] == "Defender"
    assert player["market_value_eur"] == 12000000
    assert player["height_cm"] == 181
    assert player["date_of_birth"].to_native() == date(2003, 4, 12)
    assert neo4j_db.read("MATCH (p:Player {player_id: -2}) RETURN p.position AS pos") == [{"pos": None}]

    rows = neo4j_db.read(
        "MATCH (p:Player {player_id: -3})-[:MADE]->(t:Transfer)-[:FROM]->(src:Club), (t)-[:TO]->(dst:Club) "
        "RETURN t.transfer_id AS id, t.season AS season, t.fee_eur AS fee, t.is_loan AS loan, "
        "src.name AS src, dst.name AS dst"
    )
    assert rows == [
        {
            "id": seed_data.transfer_id(-3, date(2020, 8, 1), -3, -1),
            "season": 2020,
            "fee": 1500000,
            "loan": False,
            "src": "Far Away",
            "dst": "Test A One FC",
        }
    ]
    fee = neo4j_db.read("MATCH (:Player {player_id: -1})-[:MADE]->(t) RETURN t.fee_eur AS fee")
    assert fee == [{"fee": None}]
    comp = neo4j_db.read(
        "MATCH (c:Competition {competition_id: $id})-[:IN_COUNTRY]->(k) RETURN c.name AS name, k.code AS code",
        {"id": COMPETITION},
    )
    assert comp == [{"name": "Test A League", "code": "test-a-land"}]


@pytest.mark.neo4j
def test_ingest_is_idempotent(neo4j_db, ingested, tiny_duckdb):
    before = _counts(neo4j_db)
    seed_data.ingest(neo4j_db, tiny_duckdb, [COMPETITION], FROM_SEASON, size=2)
    assert _counts(neo4j_db) == before
    assert ingested["loaded"]["players"] == 3


@pytest.mark.neo4j
def test_reingest_moves_the_current_club(neo4j_db, ingested, tiny_duckdb):
    with duckdb.connect(str(tiny_duckdb)) as con:
        con.execute("UPDATE players SET current_club_id = '-2' WHERE player_id = -1")
    seed_data.ingest(neo4j_db, tiny_duckdb, [COMPETITION], FROM_SEASON)
    rows = neo4j_db.read("MATCH (:Player {player_id: -1})-[:PLAYS_FOR]->(c) RETURN c.club_id AS club")
    assert rows == [{"club": -2}]
    assert ingested["loaded"]["plays_for"] == 3
