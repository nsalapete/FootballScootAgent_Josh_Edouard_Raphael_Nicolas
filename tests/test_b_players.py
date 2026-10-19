"""PlayerService on the mini graph (expected answers in tests/fixtures/README.md). Owner: B."""

from datetime import date

import pytest

from app.models.players import PlayerSearch
from app.services.players import (
    _TEAMMATE_PATHS,
    InvalidCursorError,
    PlayerService,
    decode_cursor,
    encode_cursor,
    fulltext_query,
)

# Left-backs of the fixture and their birth dates: ages are computed from today, never hard-coded.
LEFT_BACKS = {1: date(2003, 4, 12), 5: date(2004, 6, 21), 14: date(2005, 1, 15)}
# fmt: off
FIXTURE_VALUES = {
    1: 12_000_000, 2: 25_000_000, 3: 40_000_000, 4: 18_000_000, 5: 9_000_000, 6: 15_000_000,
    7: 6_000_000, 8: 22_000_000, 9: 11_000_000, 10: 30_000_000, 11: 14_000_000, 12: 8_000_000,
    13: 5_000_000, 14: 4_000_000, 15: 7_000_000, 16: 1_500_000,
}
# fmt: on


def age_on(birth: date, today: date) -> int:
    """Age in whole years on `today`."""
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def ids(page):
    """Player ids of a page, in order."""
    return [player.player_id for player in page.items]


@pytest.fixture(name="service")
def service_fixture(neo4j_db):
    """PlayerService bound to the test database."""
    return PlayerService(neo4j_db)


# ---------- Without a database ----------


def test_cursor_round_trip():
    cursor = encode_cursor(12_000_000, 1)
    assert "=" not in cursor
    assert decode_cursor(cursor) == (12_000_000, 1)
    assert decode_cursor(encode_cursor(-1, 99)) == (-1, 99)


@pytest.mark.parametrize("cursor", ["", "not-base64!", "e30", "WzEsMl0", "eyJ2IjoiYSIsImlkIjoxfQ", "é"])
def test_invalid_cursor_is_rejected(cursor):
    # "e30" is {}, "WzEsMl0" is [1,2], the next one is {"v":"a","id":1}.
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)


def test_fulltext_query_strips_lucene_syntax():
    assert fulltext_query("Gabriel") == "gabriel~1"
    assert fulltext_query('Rui "Almeida" AND title:*') == "rui almeida~1 and title~1"
    assert fulltext_query("+-:~()") == ""


def test_invalid_cursor_never_reaches_the_database():
    class NoDatabase:
        """Fails the test if the service queries it."""

        def read(self, query, params=None):
            raise AssertionError(f"unexpected query {query} {params}")

    with pytest.raises(InvalidCursorError):
        PlayerService(NoDatabase()).search(PlayerSearch(cursor="nope"))


# ---------- search ----------


@pytest.mark.neo4j
@pytest.mark.parametrize("max_age", [22, 23])
def test_search_left_backs_by_age(service, max_age):
    today = date.today()
    expected = {pid for pid, birth in LEFT_BACKS.items() if age_on(birth, today) <= max_age}
    page = service.search(PlayerSearch(sub_position="Left-Back", max_age=max_age))
    assert set(ids(page)) == expected
    assert page.next_cursor is None


@pytest.mark.neo4j
def test_search_left_backs_matches_readme_on_reference_day(service):
    # README: under 23 on 2026-10-19 are 5 and 14, max_age=23 adds 1. Only meaningful on that exact day.
    if date.today() != date(2026, 10, 19):
        pytest.skip("réponse du README datée du 2026-10-19")
    assert set(ids(service.search(PlayerSearch(sub_position="Left-Back", max_age=22)))) == {5, 14}
    assert set(ids(service.search(PlayerSearch(sub_position="Left-Back", max_age=23)))) == {1, 5, 14}


@pytest.mark.neo4j
def test_search_sorts_by_value_then_id(service):
    page = service.search(PlayerSearch(position="Defender", limit=100))
    assert ids(page) == [8, 11, 1, 5, 14]
    values = [player.market_value_eur for player in page.items]
    assert values == sorted(values, reverse=True)


@pytest.mark.neo4j
def test_search_returns_full_summary(service):
    page = service.search(PlayerSearch(position="Defender", sub_position="Left-Back", max_value_eur=4_000_000))
    assert len(page.items) == 1
    nathan = page.items[0]
    assert nathan.model_dump() == {
        "player_id": 14,
        "name": "Nathan Roux",
        "position": "Defender",
        "sub_position": "Left-Back",
        "date_of_birth": date(2005, 1, 15),
        "market_value_eur": 4_000_000,
        "club_id": 2,
        "club_name": "AS Montclair",
    }


@pytest.mark.neo4j
def test_search_by_max_value(service):
    page = service.search(PlayerSearch(max_value_eur=5_000_000))
    assert ids(page) == [13, 14, 16]


@pytest.mark.neo4j
def test_search_by_competition_of_current_club(service):
    # FR1 clubs are 1, 2 and 3: players 1, 6 (club 1), 13, 14 (club 2), 11, 16 (club 3).
    page = service.search(PlayerSearch(competition_id="FR1", limit=100))
    assert ids(page) == [6, 11, 1, 13, 14, 16]
    assert not service.search(PlayerSearch(competition_id="ES1")).items


@pytest.mark.neo4j
def test_search_combines_filters(service):
    page = service.search(PlayerSearch(position="Attack", competition_id="PO1", max_age=30))
    assert ids(page) == [9]


@pytest.mark.neo4j
def test_search_next_page_with_cursor(service):
    first = service.search(PlayerSearch(sub_position="Left-Back", limit=2))
    assert ids(first) == [1, 5]
    assert first.next_cursor is not None
    second = service.search(PlayerSearch(sub_position="Left-Back", limit=2, cursor=first.next_cursor))
    assert ids(second) == [14]
    assert second.next_cursor is None


@pytest.mark.neo4j
def test_search_walks_every_page_without_gap_or_duplicate(service):
    seen, cursor = [], None
    for _ in range(10):
        page = service.search(PlayerSearch(limit=5, cursor=cursor))
        seen += ids(page)
        cursor = page.next_cursor
        if cursor is None:
            break
    expected = sorted(FIXTURE_VALUES, key=lambda pid: (-FIXTURE_VALUES[pid], pid))
    assert seen == expected


@pytest.mark.neo4j
def test_search_exact_page_size_has_no_next_cursor(service):
    page = service.search(PlayerSearch(position="Goalkeeper", limit=2))
    assert ids(page) == [7, 13]
    assert page.next_cursor is None


# ---------- get, value_trend ----------


@pytest.mark.neo4j
def test_get_player_with_current_club(service):
    player = service.get(3)
    assert player.name == "Joao Ferreira"
    assert player.date_of_birth == date(2000, 2, 17)
    assert (player.club_id, player.club_name) == (4, "Kingsbridge United")


@pytest.mark.neo4j
def test_get_unknown_player(service):
    assert service.get(-424242) is None


@pytest.mark.neo4j
def test_value_trend_oldest_first(service):
    trend = service.value_trend(1)
    assert [(point.as_of, point.value_eur) for point in trend] == [
        (date(2022, 6, 30), 3_000_000),
        (date(2023, 6, 30), 7_000_000),
        (date(2024, 12, 15), 12_000_000),
    ]


@pytest.mark.neo4j
def test_value_trend_empty(service):
    assert service.value_trend(16) == []
    assert service.value_trend(-424242) == []


# ---------- resolve ----------


@pytest.mark.neo4j
def test_resolve_ambiguous_name(service):
    matches = service.resolve("Gabriel")
    assert {match.id for match in matches} == {8, 9}
    assert {match.kind for match in matches} == {"player"}
    assert all(match.score > 0 for match in matches)


@pytest.mark.neo4j
def test_resolve_full_name_ranks_exact_player_first(service):
    matches = service.resolve("Gabriel Souza")
    assert matches[0].id == 8
    assert matches[0].name == "Gabriel Souza"


@pytest.mark.neo4j
def test_resolve_tolerates_typo_and_respects_limit(service):
    assert service.resolve("Marchnd")[0].id == 1
    assert len(service.resolve("Gabriel", limit=1)) == 1


@pytest.mark.neo4j
def test_resolve_lucene_syntax_is_harmless(service):
    assert not service.resolve("+-:~()")
    assert service.resolve('Lucas "Marchand')[0].id == 1
    assert not service.resolve("Zlatan")


# ---------- teammate_path ----------


@pytest.mark.parametrize("max_hops", [0, 7, -1, True, "6", 2.5, None])
def test_teammate_path_rejects_invalid_bound_before_any_query(max_hops):
    class NoDatabase:
        """Fails the test if the service queries it."""

        def read(self, query, params=None):
            raise AssertionError(f"unexpected query {query} {params}")

    with pytest.raises(ValueError):
        PlayerService(NoDatabase()).teammate_path(1, 4, max_hops)


def test_teammate_path_queries_are_six_constants():
    assert sorted(_TEAMMATE_PATHS) == [1, 2, 3, 4, 5, 6]
    for hops, query in _TEAMMATE_PATHS.items():
        assert f"[:PLAYED_WITH*..{hops}]" in query
        assert "$from_id" in query and "$to_id" in query


@pytest.mark.neo4j
def test_teammate_path_four_hops(service):
    path = service.teammate_path(1, 4)
    assert [step.player_id for step in path.players] == [1, 6, 2, 3, 4]
    assert path.players[0].name == "Lucas Marchand"
    assert path.matches_together == [12, 10, 30, 25]
    assert path.hops == 4


@pytest.mark.neo4j
def test_teammate_path_is_undirected(service):
    path = service.teammate_path(4, 1)
    assert [step.player_id for step in path.players] == [4, 3, 2, 6, 1]


@pytest.mark.neo4j
def test_teammate_path_six_hops_is_the_limit(service):
    path = service.teammate_path(13, 8)
    assert [step.player_id for step in path.players] == [13, 1, 6, 2, 3, 4, 8]
    assert path.matches_together == [35, 12, 10, 30, 25, 33]
    assert path.hops == 6


@pytest.mark.neo4j
@pytest.mark.parametrize(
    ("from_id", "to_id"),
    [(14, 7), (1, 10), (16, 1), (-424242, 1), (1, -424242)],
    ids=["seven-hops", "separate-components", "no-teammate", "unknown-start", "unknown-end"],
)
def test_teammate_path_none(service, from_id, to_id):
    assert service.teammate_path(from_id, to_id) is None


@pytest.mark.neo4j
def test_teammate_path_respects_smaller_bound(service):
    assert service.teammate_path(1, 4, max_hops=3) is None
    assert service.teammate_path(1, 4, max_hops=4).hops == 4


@pytest.mark.neo4j
def test_teammate_path_same_player(service):
    path = service.teammate_path(16, 16)
    assert [step.player_id for step in path.players] == [16]
    assert (path.matches_together, path.hops) == ([], 0)
    assert service.teammate_path(-424242, -424242) is None
