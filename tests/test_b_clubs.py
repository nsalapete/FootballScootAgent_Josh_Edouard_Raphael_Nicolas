"""ClubService on the mini graph (expected answers in tests/fixtures/README.md). Owner: B."""

from datetime import date

import pytest

from app.services.clubs import _PIPELINE_QUERIES, ClubService

pytestmark = pytest.mark.neo4j


@pytest.fixture(name="service")
def service_fixture(neo4j_db):
    """ClubService bound to the test database."""
    return ClubService(neo4j_db)


def rows(pipeline):
    """(competition, transfers, total fee) triples, in order."""
    return [(row.competition_id, row.transfers, row.total_fee_eur) for row in pipeline]


def test_alvorada_pipeline(service):
    pipeline = service.pipeline(7)
    assert rows(pipeline) == [("GB1", 2, 50_000_000), ("FR1", 1, 9_000_000), ("PO1", 1, 0)]
    assert [row.competition_name for row in pipeline] == ["Premier League", "Ligue 1", "Liga Portugal"]


def test_pipeline_since_filters_on_transfer_date(service):
    # t01 (2022-07-15) and t02 (2021-08-01) are before the date, t06 and t07 after it.
    assert rows(service.pipeline(7, since=date(2022, 8, 1))) == [("FR1", 1, 9_000_000), ("PO1", 1, 0)]


def test_pipeline_since_is_inclusive(service):
    assert rows(service.pipeline(7, since=date(2022, 7, 15))) == [
        ("GB1", 1, 35_000_000),
        ("FR1", 1, 9_000_000),
        ("PO1", 1, 0),
    ]


def test_pipeline_limit(service):
    assert rows(service.pipeline(7, limit=1)) == [("GB1", 2, 50_000_000)]


def test_pipeline_unknown_fee_counts_as_zero(service):
    # t12: Stade Brumaire (3) to AS Montclair (2), fee unknown.
    assert rows(service.pipeline(3)) == [("FR1", 1, 0)]


def test_pipeline_uses_destination_competition(service):
    # CD Mares (8) sold to Harrowgate FC (GB1) and SC Alvorada (PO1).
    assert rows(service.pipeline(8)) == [("GB1", 1, 20_000_000), ("PO1", 1, 3_000_000)]


def test_pipeline_empty(service):
    assert not service.pipeline(4)  # Kingsbridge United never sold
    assert not service.pipeline(-424242)
    assert not service.pipeline(7, since=date(2030, 1, 1))


def test_pipeline_queries_are_constants():
    assert "$since" not in _PIPELINE_QUERIES[False]
    assert "t.date >= $since" in _PIPELINE_QUERIES[True]
    assert all("CALL (dst, t)" in query for query in _PIPELINE_QUERIES.values())


def test_resolve_club(service):
    matches = service.resolve("Kingsbridge")
    assert matches[0].id == 4
    assert matches[0].name == "Kingsbridge United"
    assert matches[0].kind == "club"


def test_resolve_club_full_name_and_typo(service):
    assert service.resolve("Wexley Athletic")[0].id == 6
    assert service.resolve("Harowgate")[0].id == 5


def test_resolve_club_limit_and_empty(service):
    assert len(service.resolve("FC Olympique Stade", limit=2)) == 2
    assert not service.resolve("!!!")
    assert not service.resolve("Barcelona")
