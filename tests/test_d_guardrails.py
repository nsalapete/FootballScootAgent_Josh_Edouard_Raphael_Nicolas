"""Tests of the Cypher validator: rejected queries with their reason, accepted queries, EXPLAIN. Owner: D."""

import pytest

from app.agent.guardrails import CypherValidator, strip_literals, tokenize

validator = CypherValidator()

REJECTED = [
    # (query, expected reason code)
    ("CREATE (p:Player {player_id: -1}) RETURN p LIMIT 1", "write"),
    ("MERGE (c:Club {club_id: 1}) RETURN c LIMIT 1", "write"),
    ("MATCH (p:Player) DELETE p RETURN count(*) LIMIT 1", "write"),
    ("MATCH (p:Player) DETACH DELETE p", "write"),
    ("MATCH (p:Player) SET p.market_value_eur = 0 RETURN p LIMIT 5", "write"),
    ("MATCH (p:Player) REMOVE p.name RETURN p LIMIT 5", "write"),
    ("DROP INDEX player_name_ft", "write"),
    ("LOAD CSV FROM 'https://evil.example/x.csv' AS row RETURN row LIMIT 5", "write"),
    ("MATCH (p:Player) FOREACH (x IN [1] | SET p.name = 'x') RETURN p LIMIT 5", "write"),
    ("match (p:Player) set p.name = 'lower-case still counts' return p limit 5", "write"),
    ("CALL dbms.listConfig() YIELD name RETURN name LIMIT 5", "procedure"),
    ("CALL apoc.cypher.runWrite('CREATE (:Player)', {}) YIELD value RETURN value LIMIT 1", "procedure"),
    ("CALL gds.pageRank.write('g', {writeProperty: 'pr'}) YIELD nodePropertiesWritten RETURN 1 LIMIT 1", "procedure"),
    ("CALL gds.graph.drop('g') YIELD graphName RETURN graphName LIMIT 1", "procedure"),
    ("CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DETACH DELETE n', {}) YIELD batches RETURN 1 LIMIT 1",
     "procedure"),
    ("SHOW USERS", "admin"),
    ("MATCH (u:User) RETURN u LIMIT 5", "label"),
    ("MATCH (p:Player)-[:OWES_MONEY_TO]->(c:Club) RETURN p LIMIT 5", "relationship"),
    ("MATCH (p) WHERE p:Secret RETURN p LIMIT 5", "label"),
    ("MATCH (p:`Player Secret`) RETURN p LIMIT 5", "label"),
    ("MATCH (a:Player)-[:PLAYED_WITH*]-(b:Player) RETURN b LIMIT 5", "unbounded_path"),
    ("MATCH (a:Player)-[:PLAYED_WITH*2..]-(b:Player) RETURN b LIMIT 5", "unbounded_path"),
    ("MATCH (a:Player)-[:PLAYED_WITH*1..12]-(b:Player) RETURN b LIMIT 5", "path_too_long"),
    ("MATCH (a:Player) ((x)-[:PLAYED_WITH]-(y))+ (b:Player) RETURN b LIMIT 5", "unbounded_path"),
    ("MATCH (a:Player) ((x)-[:PLAYED_WITH]-(y)){1,10} (b:Player) RETURN b LIMIT 5", "path_too_long"),
    ("MATCH (p:Player) RETURN p", "no_limit"),
    ("MATCH (p:Player) RETURN p LIMIT 1000", "limit_too_high"),
    ("MATCH (p:Player) RETURN p LIMIT $limit", "limit_not_literal"),
    ("MATCH (p:Player) /* LIMIT 5 */ RETURN p", "no_limit"),
    ("MATCH (p:Player) RETURN p LIMIT 5 UNION MATCH (p:Player) RETURN p", "no_limit"),
    ("MATCH (p:Player) RETURN p LIMIT 5; MATCH (n) DETACH DELETE n", "multiple_statements"),
    ("MATCH (p:Player) WHERE p.name = 'unterminated RETURN p LIMIT 5", "unterminated"),
    ("MATCH (p:Player) WITH p", "no_return"),
    ("MATCH (p:$($label)) RETURN p LIMIT 5", "dynamic"),
    ("", "empty"),
    ("MATCH (p:Player {player_id: $id}) RETURN p LIMIT 1", "parameter"),
    ("CALL gds.pageRank.stream('clubs') YIELD nodeId, score RETURN nodeId, score LIMIT 10", "procedure"),
]

ACCEPTED = [
    "MATCH (p:Player) RETURN p.name AS name LIMIT 10",
    "MATCH (p:Player {player_id: 1})-[:PLAYS_FOR]->(c:Club) RETURN c.name LIMIT 1",
    "MATCH (src:Club)<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club) RETURN src.name, dst.name, t.fee_eur LIMIT 100",
    "MATCH p = shortestPath((a:Player {player_id: 1})-[:PLAYED_WITH*..6]-(b:Player {player_id: 4})) "
    "RETURN length(p) AS hops LIMIT 1",
    "MATCH (a:Player)-[:PLAYED_WITH*1..3]-(b:Player) WHERE a.player_id = 1 RETURN DISTINCT b.name LIMIT 20",
    # Write keywords inside strings and comments are ignored.
    "MATCH (p:Player) WHERE p.name = 'DROP; CREATE (x) DETACH DELETE' RETURN p LIMIT 5 // then DELETE everything",
    "MATCH (p:Player) WHERE p.name CONTAINS \"O\\\"Brien; SET\" RETURN p LIMIT 5",
    "MATCH (c:Club) RETURN c {.name, total: COUNT { (c)<-[:FROM]-(:Transfer) }} AS club LIMIT 5",
    "MATCH (p:Player) WHERE p:Player OR p:Club RETURN [x IN [p] WHERE x:Player | x.name] AS names LIMIT 5",
    "CALL db.index.fulltext.queryNodes('player_name_ft', 'Gabriel') YIELD node, score "
    "RETURN node.name, score LIMIT 5",
    "MATCH (start:Player)-[:MADE]->(t:Transfer) WHERE t.date >= date('2022-01-01') "
    "RETURN start.name, t.fee_eur * 2 AS double_fee ORDER BY double_fee DESC LIMIT 10;",
    "MATCH (r:Rumour)-[:ABOUT]->(p:Player) RETURN r.rumour_id, p.name LIMIT 5 "
    "UNION MATCH (r:Rumour)-[:TARGET]->(p:Club) RETURN r.rumour_id, p.name LIMIT 5",
    "MATCH (p:Player)-[m:PREDICTED_MOVE]->(c:Club) RETURN p.name, c.name, m.prob LIMIT 5",
]


@pytest.mark.parametrize(("query", "code"), REJECTED)
def test_rejected_queries_give_the_expected_reason(query, code):
    result = validator.validate(query)
    assert not result.valid
    assert any(reason.startswith(f"{code}:") for reason in result.reasons), result.reasons


@pytest.mark.parametrize("query", ACCEPTED)
def test_accepted_queries(query):
    result = validator.validate(query)
    assert result.valid, result.reasons
    assert result.reasons == []


def test_enough_cases():
    assert len(REJECTED) >= 20
    assert len(ACCEPTED) >= 10


def test_every_broken_rule_is_reported():
    result = validator.validate("MATCH (u:User)-[:PLAYED_WITH*]-(p:Player) SET p.name = 'x' RETURN p")
    codes = {reason.split(":")[0] for reason in result.reasons}
    assert {"label", "unbounded_path", "write", "no_limit"} <= codes


def test_trailing_semicolon_is_dropped_from_the_returned_query():
    result = validator.validate("MATCH (p:Player) RETURN p LIMIT 5;")
    assert result.valid
    assert result.query == "MATCH (p:Player) RETURN p LIMIT 5"


def test_too_long_query_is_rejected():
    result = validator.validate("MATCH (p:Player) RETURN p LIMIT 5 " + "// padding " * 300)
    assert result.reasons[0].startswith("too_long:")


def test_custom_bounds():
    strict = CypherValidator(max_hops=2, max_limit=10)
    assert not strict.validate("MATCH (a:Player)-[:PLAYED_WITH*..3]-(b) RETURN b LIMIT 5").valid
    assert not strict.validate("MATCH (p:Player) RETURN p LIMIT 11").valid
    assert strict.validate("MATCH (a:Player)-[:PLAYED_WITH*..2]-(b) RETURN b LIMIT 10").valid


def test_strip_literals_blanks_strings_and_comments():
    text, problems = strip_literals("MATCH (p) WHERE p.name = 'CREATE' /* DELETE */ RETURN p // SET")
    assert problems == []
    assert "CREATE" not in text and "DELETE" not in text and "SET" not in text
    assert tokenize("a.b..3 <-[*1..6]->")[:3] == ["a", ".", "b"]


@pytest.mark.neo4j
@pytest.mark.parametrize("query", ACCEPTED[:6])
def test_accepted_queries_pass_explain_on_the_test_database(neo4j_db, query):
    result = validator.validate(query)
    neo4j_db.read("EXPLAIN " + result.query)


@pytest.mark.neo4j
def test_read_transaction_is_a_second_safety_net(neo4j_db):
    with pytest.raises(Exception):  # pylint: disable=broad-exception-caught
        neo4j_db.read("MATCH (p:Player {player_id: -999}) SET p.name = 'test-d-never' RETURN p LIMIT 1")
