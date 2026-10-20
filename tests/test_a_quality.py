"""Tests of the data-quality rules (scripts/data_quality.py) and of their effect on the ingestion. Owner: A."""

from datetime import date

import duckdb
import pytest

from scripts import seed_data
from scripts.data_quality import clean_transfers, drop_reason, mark_loans, split_duplicates
from tests.test_a_ingest import CLEANUP, DUCKDB_TABLES

TODAY = date(2026, 10, 20)
FROM_SEASON = 2020


def move(player, day, src, dst, fee=0, season=None):
    """A shaped transfer row, as `seed_data.shape_transfer` builds it."""
    return {
        "transfer_id": seed_data.transfer_id(player, day, src, dst),
        "player_id": player,
        "from_club_id": src,
        "to_club_id": dst,
        "date": day,
        "season": season if season is not None else (day.year if day.month >= 7 else day.year - 1),
        "fee_eur": fee,
        "is_loan": False,
    }


# ---------------------------------------------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------------------------------------------


def test_out_and_back_without_fee_is_a_loan():
    rows = mark_loans([move(1, date(2022, 8, 1), 10, 20), move(1, date(2023, 6, 30), 20, 10, fee=None)])
    assert [row["is_loan"] for row in rows] == [True, True]


def test_paid_return_is_not_a_loan():
    rows = mark_loans([move(1, date(2022, 8, 1), 10, 20), move(1, date(2023, 6, 30), 20, 10, fee=5_000_000)])
    assert [row["is_loan"] for row in rows] == [False, False]


def test_two_season_loan_is_a_loan_but_not_a_longer_one():
    two_seasons = mark_loans([move(1, date(2020, 9, 22), 10, 20), move(1, date(2022, 6, 30), 20, 10)])
    longer = mark_loans([move(1, date(2020, 7, 1), 10, 20), move(1, date(2022, 9, 1), 20, 10)])
    assert [row["is_loan"] for row in two_seasons] == [True, True]
    assert [row["is_loan"] for row in longer] == [False, False]


def test_purchase_then_loan_back_flags_only_the_loan():
    rows = mark_loans(
        [
            move(1, date(2021, 8, 12), 10, 20, fee=113_000_000),  # 10 sells to 20
            move(1, date(2022, 7, 1), 20, 10),  # 20 loans him back to 10
            move(1, date(2023, 6, 30), 10, 20),  # end of loan
        ]
    )
    assert [row["is_loan"] for row in rows] == [False, True, True]


def test_two_loans_in_a_row():
    rows = mark_loans(
        [
            move(1, date(2025, 2, 2), 10, 20),
            move(1, date(2025, 8, 10), 20, 10),
            move(1, date(2025, 8, 12), 10, 30),
            move(1, date(2026, 6, 30), 30, 10),
        ]
    )
    assert all(row["is_loan"] for row in rows)


def test_only_consecutive_moves_of_the_same_player_count():
    rows = mark_loans(
        [
            move(1, date(2022, 8, 1), 10, 20),
            move(2, date(2022, 12, 1), 20, 10),  # another player
            move(1, date(2023, 1, 10), 20, 30),  # player 1 moves on instead of coming back
            move(1, date(2023, 6, 30), 30, 10),
        ]
    )
    assert not any(row["is_loan"] for row in rows)


def test_mark_loans_does_not_mutate_its_input():
    original = [move(1, date(2022, 8, 1), 10, 20), move(1, date(2023, 6, 30), 20, 10)]
    mark_loans(original)
    assert not any(row["is_loan"] for row in original)


def test_planned_return_marks_the_loan_but_is_dropped():
    rows = [move(1, date(2026, 8, 1), 10, 20), move(1, date(2027, 6, 30), 20, 10)]
    result = clean_transfers(rows, FROM_SEASON, TODAY)
    assert [(row["to_club_id"], row["is_loan"]) for row in result.rows] == [(20, True)]
    assert result.counts["future"] == 1
    assert result.dropped_ids == [rows[1]["transfer_id"]]


# ---------------------------------------------------------------------------------------------------------------
# Drops
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (move(1, date(2019, 8, 1), 10, 20), "before_scope"),
        (move(1, date(2021, 8, 1), 10, 20, season=None) | {"season": None}, "before_scope"),
        (move(1, date(2027, 1, 1), 10, 20), "future"),
        (move(1, date(2021, 8, 1), 10, 10), "same_club"),
        (move(1, date(2021, 8, 1), 10, 515), "pseudo_club"),
        (move(1, date(2021, 8, 1), 123, 10), "pseudo_club"),
        (move(1, date(2021, 8, 1), 10, 20), None),
    ],
)
def test_drop_reason(row, reason):
    assert drop_reason(row, FROM_SEASON, TODAY) == reason


def test_duplicates_keep_the_best_known_fee():
    rows = [
        move(1, date(2015, 6, 30), 10, 20, fee=0),
        move(1, date(2015, 7, 2), 10, 20, fee=15_000_000),
        move(1, date(2015, 7, 20), 10, 20, fee=None),
        move(1, date(2016, 7, 1), 10, 20, fee=None),  # a year later: a new move
    ]
    kept, duplicates = split_duplicates(rows)
    assert sorted((row["date"], row["fee_eur"]) for row in kept) == [
        (date(2015, 7, 2), 15_000_000),
        (date(2016, 7, 1), None),
    ]
    assert len(duplicates) == 2


def test_clean_transfers_counts_every_case():
    rows = [
        move(1, date(2019, 8, 1), 10, 20),  # before the scope
        move(1, date(2021, 8, 1), 10, 20),  # loan
        move(1, date(2022, 5, 31), 20, 10),  # end of loan
        move(2, date(2021, 8, 1), 10, 515),  # released: Without Club
        move(3, date(2021, 8, 1), 30, 30),  # same club
        move(4, date(2021, 8, 1), 30, 40, fee=None),
        move(4, date(2021, 8, 10), 30, 40, fee=2_000_000),  # duplicate record, better fee
        move(5, date(2028, 2, 2), 30, 40),  # future
    ]
    result = clean_transfers(rows, FROM_SEASON, TODAY)
    assert result.counts == {
        "raw": 8,
        "before_scope": 1,
        "future": 1,
        "same_club": 1,
        "pseudo_club": 1,
        "duplicate": 1,
        "kept": 3,
        "loans": 2,
        "unknown_fee": 0,
        "zero_fee": 2,
    }
    assert len(result.dropped_ids) == 4  # before_scope rows were never loaded: nothing to prune
    assert [row["player_id"] for row in result.rows] == [1, 1, 4]


def test_transfer_rows_uses_today():
    raw = [
        {
            "player_id": 1,
            "transfer_date": date(2026, 9, 1),
            "transfer_season": "26/27",
            "from_club_id": 10,
            "to_club_id": 20,
            "from_club_name": "Ten",
            "to_club_name": "Twenty",
            "transfer_fee": None,
        }
    ]
    assert len(seed_data.transfer_rows(raw, FROM_SEASON, today=date(2026, 10, 20))) == 1
    assert not seed_data.transfer_rows(raw, FROM_SEASON, today=date(2026, 8, 1))


def test_player_with_a_placeholder_club_has_no_club():
    item = {
        "player_id": 1,
        "name": "Free Agent",
        "position": "Attack",
        "sub_position": None,
        "date_of_birth": None,
        "market_value_in_eur": None,
        "foot": None,
        "height_in_cm": None,
        "club_id": 515,
        "current_club_name": None,
        "country_of_citizenship": None,
    }
    assert seed_data.player_row(item)["club_id"] is None
    assert not seed_data.light_club_rows([], [item], set())


# ---------------------------------------------------------------------------------------------------------------
# Integration: dirty rows in the tiny DuckDB file
# ---------------------------------------------------------------------------------------------------------------

DIRTY_ROWS = """
INSERT INTO transfers VALUES
    (-1, '2022-08-01', '22/23', -1, -3, 'One', 'Far Away', 0),
    (-1, '2023-06-30', '22/23', -3, -1, 'Far Away', 'One', 0),
    (-2, '2022-07-01', '22/23', -2, 515, 'Two', 'Without Club', NULL),
    (-3, '2030-06-30', '29/30', -1, -2, 'One', 'Two', 0),
    (-3, '2020-08-20', '20/21', -3, -1, 'Far Away', 'One', NULL),
    (-1, '2021-03-01', '20/21', -2, -2, 'Two', 'Two', 0);
"""
PSEUDO_TRANSFER_ID = seed_data.transfer_id(-2, date(2022, 7, 1), -2, 515)


@pytest.fixture()
def dirty_duckdb(tmp_path):
    """The tiny DuckDB file of test_a_ingest plus one row of each data-quality case."""
    path = tmp_path / "dirty.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(DUCKDB_TABLES)
        con.execute(DIRTY_ROWS)
    return path


def _cleanup(db):
    for query in CLEANUP:
        db.write(query)
    db.write("MATCH (t:Transfer {transfer_id: $id}) DETACH DELETE t", {"id": PSEUDO_TRANSFER_ID})
    db.write("MATCH (c:Club {club_id: 515}) WHERE c.name STARTS WITH 'test-a-' DETACH DELETE c")


def test_extract_reports_the_quality_counts(dirty_duckdb):
    data = seed_data.extract(dirty_duckdb, ["test-a-league"], FROM_SEASON, TODAY)
    assert data.cleaning.counts == {
        "raw": 10,
        "before_scope": 1,
        "future": 1,
        "same_club": 1,
        "pseudo_club": 1,
        "duplicate": 1,
        "kept": 5,
        "loans": 2,
        "unknown_fee": 1,
        "zero_fee": 3,
        "players_without_club": 0,
    }
    assert 515 not in data.club_ids()


@pytest.mark.neo4j
def test_ingest_prunes_rows_excluded_by_a_rule(neo4j_db, dirty_duckdb):
    _cleanup(neo4j_db)
    # What an older run, without the quality rules, could have left in the graph.
    neo4j_db.write(
        "CREATE (:Transfer {transfer_id: $id})-[:TO]->(:Club {club_id: 515, name: 'test-a-Without Club'})",
        {"id": PSEUDO_TRANSFER_ID},
    )
    try:
        report = seed_data.ingest(neo4j_db, dirty_duckdb, ["test-a-league"], FROM_SEASON, TODAY)
        assert report["quality"]["kept"] == 5
        assert not neo4j_db.read("MATCH (c:Club {club_id: 515}) RETURN c")
        assert not neo4j_db.read("MATCH (t:Transfer {transfer_id: $id}) RETURN t", {"id": PSEUDO_TRANSFER_ID})
        loans = neo4j_db.read(
            "MATCH (:Player {player_id: -1})-[:MADE]->(t:Transfer) WHERE t.is_loan " "RETURN t.date AS day ORDER BY day"
        )
        assert [row["day"].to_native() for row in loans] == [date(2022, 8, 1), date(2023, 6, 30)]
        kept = neo4j_db.read("MATCH (:Player {player_id: -3})-[:MADE]->(t:Transfer) RETURN t.fee_eur AS fee")
        assert kept == [{"fee": 1500000}]  # the duplicate without fee was dropped, the future move too
    finally:
        _cleanup(neo4j_db)
