"""Checks that the mini graph matches tests/fixtures/README.md. Owner: A."""

import pytest

pytestmark = pytest.mark.neo4j


def count(db, query):
    return db.read(query)[0]["n"]


def test_node_counts(neo4j_db):
    assert count(neo4j_db, "MATCH (p:Player) RETURN count(p) AS n") == 16
    assert count(neo4j_db, "MATCH (c:Club) RETURN count(c) AS n") == 8
    assert count(neo4j_db, "MATCH (t:Transfer) RETURN count(t) AS n") == 12
    assert count(neo4j_db, "MATCH ()-[r:SENDS_TO]->() RETURN count(r) AS n") == 12
    assert count(neo4j_db, "MATCH ()-[r:PLAYED_WITH]->() RETURN count(r) AS n") == 14


def test_top_seller_is_alvorada(neo4j_db):
    rows = neo4j_db.read(
        "MATCH (c:Club)<-[:FROM]-(t:Transfer) RETURN c.club_id AS club, count(t) AS n ORDER BY n DESC LIMIT 1"
    )
    assert rows == [{"club": 7, "n": 4}]


def test_alvorada_pipeline_by_competition(neo4j_db):
    rows = neo4j_db.read(
        "MATCH (:Club {club_id: 7})<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club) "
        "MATCH (dst)-[:PLAYS_IN {season: t.season}]->(comp:Competition) "
        "RETURN comp.competition_id AS comp, count(t) AS n, sum(coalesce(t.fee_eur, 0)) AS fee "
        "ORDER BY n DESC, comp"
    )
    assert rows == [
        {"comp": "GB1", "n": 2, "fee": 50000000},
        {"comp": "FR1", "n": 1, "fee": 9000000},
        {"comp": "PO1", "n": 1, "fee": 0},
    ]


def test_teammate_path_lengths(neo4j_db):
    query = (
        "MATCH (a:Player {player_id: $a}), (b:Player {player_id: $b}) "
        "MATCH p = shortestPath((a)-[:PLAYED_WITH*..6]-(b)) RETURN length(p) AS n"
    )
    assert neo4j_db.read(query, {"a": 1, "b": 4}) == [{"n": 4}]
    assert neo4j_db.read(query, {"a": 13, "b": 8}) == [{"n": 6}]
    assert not neo4j_db.read(query, {"a": 14, "b": 7})
    assert not neo4j_db.read(query, {"a": 1, "b": 10})


def test_read_transaction_rejects_writes(neo4j_db):
    with pytest.raises(Exception):  # pylint: disable=broad-exception-caught
        neo4j_db.read("CREATE (:Player {player_id: -1})")
