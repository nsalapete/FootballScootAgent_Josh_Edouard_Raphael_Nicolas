"""ml.dataset on small synthetic data (no Neo4j, no real DuckDB file). Owner: C."""

import math
from datetime import date

import duckdb
import pandas as pd
import pytest

from ml.dataset import (
    FEATURES,
    Universe,
    build_split,
    load_universe,
    new_pairs,
    pair_features,
    sample_negatives,
    training_graph,
)

# Clubs 1-6 in scope, 99 in another league. Seasons 2019-2021 are the past, 2022-2023 the target window.
EDGES = pd.DataFrame(
    [
        (1, 2, 2019, 2),
        (2, 3, 2019, 1),
        (1, 3, 2020, 1),
        (3, 4, 2020, 1),
        (4, 5, 2021, 3),
        (5, 6, 2021, 1),
        (2, 4, 2021, 1),
        (1, 4, 2022, 1),  # new pair in the window
        (6, 4, 2023, 1),  # new pair in the window
        (2, 1, 2022, 1),  # new direction of an existing undirected link: still a new directed pair
        (1, 2, 2023, 1),  # pair already seen in the past: not a positive
        (1, 7, 2022, 1),  # club 7 unseen before 2022: no neighbourhood, not a positive
        (5, 1, 2024, 1),  # after the window: not a positive (the label only covers 2022-2023)
    ],
    columns=["src", "dst", "season", "count"],
)
COUNTRIES = {1: "France", 2: "France", 3: "England", 4: "England", 5: "Spain", 6: "Spain"}
SPLIT = (2021, (2022, 2023))
UNIVERSE = Universe(EDGES, countries=COUNTRIES)  # no scope given: every graph club is a candidate


def test_training_graph_stops_at_the_cutoff():
    graph = training_graph(EDGES, 2020)
    assert set(graph.edges) == {(1, 2), (2, 3), (1, 3), (3, 4)}
    assert graph[1][2]["count"] == 2


def test_new_pairs():
    graph = training_graph(EDGES, SPLIT[0])
    assert new_pairs(EDGES, graph, SPLIT[1]) == {(1, 4), (6, 4), (2, 1)}


def test_no_leak_between_graph_and_labels():
    split = build_split(UNIVERSE, SPLIT)
    graph = training_graph(EDGES, SPLIT[0])
    positives = split[split["label"] == 1]
    assert not any(graph.has_edge(src, dst) for src, dst in zip(positives["src"], positives["dst"]))
    # Features only depend on the past: dropping every future edge changes nothing.
    past_only = build_split(Universe(EDGES[EDGES["season"] <= SPLIT[0]], countries=COUNTRIES), SPLIT)
    assert past_only.empty  # no label without the future...
    past_graph = training_graph(EDGES[EDGES["season"] <= SPLIT[0]], SPLIT[0])
    features = pair_features(past_graph, list(zip(split["src"], split["dst"])), COUNTRIES)
    pd.testing.assert_frame_equal(features, split.drop(columns="label"))  # ...but identical features


def test_negatives_count_seed_and_exclusions():
    split = build_split(UNIVERSE, SPLIT, negatives_per_positive=5)
    negatives = split[split["label"] == 0]
    assert split["label"].sum() == 3
    assert len(negatives) == 15
    window = EDGES[EDGES["season"] <= 2023]
    traded = set(zip(window["src"], window["dst"]))
    assert not traded & set(zip(negatives["src"], negatives["dst"]))
    assert all(src != dst for src, dst in zip(negatives["src"], negatives["dst"]))
    pd.testing.assert_frame_equal(split, build_split(UNIVERSE, SPLIT, negatives_per_positive=5))


def test_negatives_capped_by_free_pairs():
    graph = training_graph(EDGES, 2019)  # 3 clubs, 2 edges: 6 - 2 = 4 free pairs
    assert len(sample_negatives(graph, set(), 100)) == 4
    assert sample_negatives(graph, set(), 2, seed=1) == sample_negatives(graph, set(), 2, seed=1)


def test_pair_features_by_hand():
    graph = training_graph(EDGES, SPLIT[0])
    frame = pair_features(graph, [(1, 4), (6, 1)], COUNTRIES).set_index(["src", "dst"])
    assert list(frame.columns) == FEATURES
    # Undirected neighbours: 1 -> {2, 3}, 4 -> {2, 3, 5}; degree(2) = degree(3) = 3.
    assert frame.loc[(1, 4), "common_neighbours"] == 2
    assert frame.loc[(1, 4), "adamic_adar"] == pytest.approx(2 / math.log(3))
    assert frame.loc[(1, 4), "preferential_attachment"] == 2 * 3
    assert frame.loc[(1, 4), "same_country"] == 0
    assert frame.loc[(6, 1), "common_neighbours"] == 0
    assert frame.loc[(6, 1), "adamic_adar"] == 0
    assert frame.loc[(1, 4), "pagerank_gap"] >= 0
    same = pair_features(graph, [(1, 2), (5, 6)], COUNTRIES)
    assert list(same["same_country"]) == [1, 1]


def test_scope_restricts_candidate_pairs():
    # Clubs 5 and 6 are light clubs: they shape the features but never appear in a sample.
    universe = Universe(EDGES, clubs=frozenset({1, 2, 3, 4}), countries=COUNTRIES)
    graph = training_graph(EDGES, SPLIT[0])
    assert new_pairs(EDGES, graph, SPLIT[1], universe.clubs) == {(1, 4), (2, 1)}
    split = build_split(universe, SPLIT)
    assert set(split["src"]) | set(split["dst"]) <= {1, 2, 3, 4}
    assert split["label"].sum() == 2
    # 4 x 3 = 12 pairs, minus 5 past edges among 1-4 and 2 window pairs (1, 4), (2, 1): 5 free pairs.
    assert (split["label"] == 0).sum() == 5


@pytest.fixture(name="duckdb_file")
def duckdb_file_fixture(tmp_path):
    """A tiny DuckDB file with the Transfermarkt tables read by the ingestion queries of A."""
    path = tmp_path / "mini.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE competitions (competition_id VARCHAR, country_name VARCHAR)")
        con.execute("INSERT INTO competitions VALUES ('FR1', 'France'), ('GB1', 'England'), ('XX1', 'Elsewhere')")
        con.execute("CREATE TABLE clubs (club_id VARCHAR, name VARCHAR)")
        con.execute("INSERT INTO clubs VALUES ('1', 'One'), ('2', 'Two'), ('3', 'Three'), ('4', 'Four')")
        con.execute(
            "CREATE TABLE games (game_id VARCHAR, competition_id VARCHAR, season VARCHAR, home_club_id INTEGER, "
            "away_club_id INTEGER, home_club_name VARCHAR, away_club_name VARCHAR)"
        )
        # Club 3 played FR1 then GB1 (country = latest league), club 5 only before the scope, club 6 elsewhere.
        con.execute(
            "INSERT INTO games VALUES ('1', 'FR1', '2021', 1, 2, 'One', 'Two'), ('2', 'FR1', '2020', 3, 1, "
            "'Three', 'One'), ('3', 'GB1', '2022', 3, 4, 'Three', 'Four'), ('4', 'FR1', '2014', 5, 1, 'Five', "
            "'One'), ('5', 'XX1', '2022', 6, 99, 'Six', 'Far')"
        )
        con.execute("CREATE TABLE players (player_id INTEGER)")
        con.execute("INSERT INTO players SELECT range FROM range(10, 20)")
        con.execute(
            "CREATE TABLE transfers (player_id INTEGER, transfer_date DATE, transfer_season VARCHAR, "
            "from_club_id INTEGER, to_club_id INTEGER, from_club_name VARCHAR, to_club_name VARCHAR, "
            "transfer_fee DECIMAL(18, 3))"
        )
        con.execute(
            "INSERT INTO transfers VALUES "
            "(10, '2021-07-01', '21/22', 1, 2, 'One', 'Two', 1000), "
            "(11, '2021-08-01', '21/22', 1, 2, 'One', 'Two', 2000), "
            "(12, '2022-07-01', '22/23', 2, 3, 'Two', 'Three', 5), "
            "(13, '2022-07-01', '22/23', 3, 99, 'Three', 'Far', 0), "  # light club on one side: kept
            "(14, '2022-07-01', '22/23', 5, 99, 'Five', 'Far', 0), "  # no scope club: not read
            "(15, '2022-07-01', '22/23', 1, 515, 'One', 'Without Club', 0), "  # placeholder: dropped
            "(16, '2014-07-01', '14/15', 2, 1, 'Two', 'One', 0), "  # before the scope: dropped
            "(17, '2025-07-01', '25/26', 4, 1, 'Four', 'One', 0), "  # after `today`: dropped
            "(18, '2022-07-01', '', 2, 4, 'Two', 'Four', 0)"  # unreadable season: dropped, no crash
        )
    return path


def test_load_universe_uses_the_ingestion_scope(duckdb_file):
    universe = load_universe(duckdb_file, ["FR1", "GB1"], from_season=2015, today=date(2024, 12, 31))
    assert universe.clubs == frozenset({1, 2, 3, 4})
    assert universe.countries == {1: "France", 2: "France", 3: "England", 4: "England"}
    edges = sorted(universe.edges.itertuples(index=False, name=None))
    assert edges == [(1, 2, 2021, 2), (2, 3, 2022, 1), (3, 99, 2022, 1)]


def test_sliding_feature_window_keeps_labels_on_the_full_history():
    # Window of 1 season: features only see 2021; "new" is still judged on 2019-2021.
    windowed = build_split(Universe(EDGES, countries=COUNTRIES, feature_window=1), SPLIT)
    full = build_split(UNIVERSE, SPLIT)
    pd.testing.assert_frame_equal(windowed[["src", "dst", "label"]], full[["src", "dst", "label"]])
    graph = training_graph(EDGES, SPLIT[0], window=1)
    assert set(graph.edges) == {(4, 5), (5, 6), (2, 4)}
    # (1, 4): common neighbours 2 and 3 on the full history, none in 2021 alone.
    assert windowed.set_index(["src", "dst"]).loc[(1, 4), "common_neighbours"] == 0
