"""Tests of the rumour confirmation job (scripts/confirm_rumours.py). Owner: A.

Integration tests create their own rumours (`test-a-` ids), players and clubs (negative ids) and always pass
`rumour_ids`: r1 and r2 of the mini graph, which D's tests rely on, are never processed.
"""

from datetime import date

import pytest

from scripts.confirm_rumours import confirm_rumours, decide

TODAY = date(2026, 10, 20)
PUBLISHED = date(2024, 1, 10)
TARGET = 6


def move(transfer_id, day, club_id):
    return {"transfer_id": transfer_id, "date": day, "club_id": club_id}


# ---------------------------------------------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------------------------------------------


def test_move_to_the_target_in_the_window_confirms():
    assert decide(PUBLISHED, TODAY, [move("t1", date(2024, 3, 1), TARGET)], TARGET) == ("confirmed", "t1")


def test_move_elsewhere_contradicts():
    assert decide(PUBLISHED, TODAY, [move("t1", date(2024, 2, 1), 9)], TARGET) == ("contradicted", None)


def test_target_after_a_move_elsewhere_still_confirms():
    transfers = [move("t2", date(2024, 5, 1), TARGET), move("t1", date(2024, 2, 1), 9)]
    assert decide(PUBLISHED, TODAY, transfers, TARGET) == ("confirmed", "t2")


def test_window_bounds_are_inclusive():
    assert decide(PUBLISHED, TODAY, [move("t1", PUBLISHED, TARGET)], TARGET)[0] == "confirmed"
    assert decide(PUBLISHED, TODAY, [move("t1", date(2024, 7, 8), TARGET)], TARGET)[0] == "confirmed"  # day 180
    assert decide(PUBLISHED, TODAY, [move("t1", date(2024, 7, 9), TARGET)], TARGET)[0] == "expired"  # day 181


def test_move_before_the_article_does_not_count():
    assert decide(PUBLISHED, TODAY, [move("t1", date(2023, 12, 31), TARGET)], TARGET) == ("expired", None)


def test_expiry_after_a_year_without_move():
    # 2024 is a leap year: day 365 after 2024-01-10 is 2025-01-09, the last day still pending.
    assert decide(PUBLISHED, date(2025, 1, 9), [], TARGET) == ("pending", None)
    assert decide(PUBLISHED, date(2025, 1, 10), [], TARGET) == ("expired", None)


def test_recent_rumour_stays_pending():
    assert decide(date(2026, 9, 1), TODAY, [], TARGET) == ("pending", None)


def test_future_moves_are_ignored():
    # A move dated after `today` is not known yet: the rumour is neither confirmed nor contradicted.
    assert decide(date(2026, 9, 1), TODAY, [move("t1", date(2026, 12, 1), 9)], TARGET) == ("pending", None)


def test_undated_rumour_stays_pending():
    assert decide(None, TODAY, [move("t1", date(2024, 3, 1), TARGET)], TARGET) == ("pending", None)


# ---------------------------------------------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------------------------------------------

SETUP = """
CREATE (target:Club {club_id: -201, name: 'test-a target'}), (other:Club {club_id: -202, name: 'test-a other'}),
       (home:Club {club_id: -203, name: 'test-a home'})
WITH target, other, home
UNWIND [
  {player: -201, rumour: 'test-a-confirmed', published: '2024-01-10', status: 'pending',
   moves: [{id: 'test-a-t1', day: '2024-03-01', to: -201}]},
  {player: -202, rumour: 'test-a-contradicted', published: '2024-01-10', status: 'pending',
   moves: [{id: 'test-a-t2', day: '2024-02-01', to: -202}]},
  {player: -203, rumour: 'test-a-expired', published: '2024-01-10', status: 'pending',
   moves: [{id: 'test-a-t3', day: '2024-09-01', to: -201}]},
  {player: -204, rumour: 'test-a-pending', published: '2026-09-01', status: 'pending', moves: []},
  {player: -205, rumour: 'test-a-done', published: '2024-01-10', status: 'contradicted',
   moves: [{id: 'test-a-t5', day: '2024-03-01', to: -201}]}
] AS row
CREATE (p:Player {player_id: row.player, name: 'test-a ' + row.rumour})
CREATE (r:Rumour {rumour_id: row.rumour, status: row.status, confidence: 0.5})
CREATE (:Article {url: 'https://example.org/test-a/' + row.rumour, source: 'test-a',
                  published_at: date(row.published)})-[:REPORTS]->(r)
CREATE (r)-[:ABOUT]->(p), (r)-[:TARGET]->(target)
WITH p, home, row
UNWIND row.moves AS m
MATCH (dst:Club {club_id: m.to})
CREATE (p)-[:MADE]->(t:Transfer {transfer_id: m.id, date: date(m.day), season: 2023, fee_eur: null,
                                  is_loan: false})
CREATE (t)-[:FROM]->(home), (t)-[:TO]->(dst)
"""

UNDATED = """
MATCH (p:Player {player_id: -201}), (target:Club {club_id: -201})
CREATE (r:Rumour {rumour_id: 'test-a-undated', status: 'pending', confidence: 0.5})
CREATE (r)-[:ABOUT]->(p), (r)-[:TARGET]->(target)
"""

CLEANUP = [
    "MATCH (a:Article) WHERE a.source = 'test-a' DETACH DELETE a",
    "MATCH (r:Rumour) WHERE r.rumour_id STARTS WITH 'test-a-' DETACH DELETE r",
    "MATCH (t:Transfer) WHERE t.transfer_id STARTS WITH 'test-a-' DETACH DELETE t",
    "MATCH (n) WHERE (n:Player AND n.player_id <= -201 AND n.player_id >= -205) "
    "OR (n:Club AND n.club_id <= -201 AND n.club_id >= -203) DETACH DELETE n",
]

IDS = ["test-a-confirmed", "test-a-contradicted", "test-a-expired", "test-a-pending", "test-a-done", "test-a-undated"]
STATUS = "MATCH (r:Rumour) WHERE r.rumour_id IN $ids RETURN r.rumour_id AS id, r.status AS status ORDER BY id"


@pytest.fixture()
def rumours(neo4j_db):
    """Six test rumours, one per case, removed afterwards."""
    for query in CLEANUP:
        neo4j_db.write(query)
    neo4j_db.write(SETUP)
    neo4j_db.write(UNDATED)
    yield neo4j_db
    for query in CLEANUP:
        neo4j_db.write(query)


@pytest.mark.neo4j
def test_confirm_rumours_updates_each_case(rumours):
    counts = confirm_rumours(rumours, TODAY, IDS)
    assert counts == {"checked": 5, "confirmed": 1, "contradicted": 1, "expired": 1, "pending": 2}
    assert {row["id"]: row["status"] for row in rumours.read(STATUS, {"ids": IDS})} == {
        "test-a-confirmed": "confirmed",
        "test-a-contradicted": "contradicted",
        "test-a-done": "contradicted",
        "test-a-expired": "expired",
        "test-a-pending": "pending",
        "test-a-undated": "pending",
    }
    proof = rumours.read(
        "MATCH (r:Rumour)-[:CONFIRMED_BY]->(t:Transfer) WHERE r.rumour_id STARTS WITH 'test-a-' "
        "RETURN r.rumour_id AS rumour, t.transfer_id AS transfer, r.resolved_on AS day"
    )
    assert [(row["rumour"], row["transfer"], row["day"].to_native()) for row in proof] == [
        ("test-a-confirmed", "test-a-t1", TODAY)
    ]


@pytest.mark.neo4j
def test_confirm_rumours_is_idempotent(rumours):
    confirm_rumours(rumours, TODAY, IDS)
    assert confirm_rumours(rumours, TODAY, IDS) == {
        "checked": 2,
        "confirmed": 0,
        "contradicted": 0,
        "expired": 0,
        "pending": 2,
    }
    links = rumours.read("MATCH (r:Rumour)-[c:CONFIRMED_BY]->() WHERE r.rumour_id = 'test-a-confirmed' RETURN c")
    assert len(links) == 1


@pytest.mark.neo4j
def test_rumour_ids_limit_the_run_and_spare_the_mini_graph(rumours):
    before = rumours.read(STATUS, {"ids": ["r1", "r2"]})
    assert confirm_rumours(rumours, TODAY, ["test-a-expired"])["checked"] == 1
    assert rumours.read(STATUS, {"ids": ["test-a-confirmed"]}) == [{"id": "test-a-confirmed", "status": "pending"}]
    assert (
        rumours.read(STATUS, {"ids": ["r1", "r2"]})
        == before
        == [
            {"id": "r1", "status": "pending"},
            {"id": "r2", "status": "confirmed"},
        ]
    )
