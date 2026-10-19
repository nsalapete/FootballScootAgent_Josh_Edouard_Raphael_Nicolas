"""ml.dataset on small synthetic data (no Neo4j, no real DuckDB file). Owner: C."""

import math

import duckdb
import pandas as pd
import pytest

from ml.dataset import (
    FEATURES,
    build_split,
    load_countries,
    load_edges,
    new_pairs,
    pair_features,
    parse_season,
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


def test_parse_season():
    assert parse_season("22/23") == 2022
    assert parse_season("00/01") == 2000
    assert parse_season("99/00") == 1999


def test_training_graph_stops_at_the_cutoff():
    graph = training_graph(EDGES, 2020)
    assert set(graph.edges) == {(1, 2), (2, 3), (1, 3), (3, 4)}
    assert graph[1][2]["count"] == 2


def test_new_pairs():
    graph = training_graph(EDGES, SPLIT[0])
    assert new_pairs(EDGES, graph, SPLIT[1]) == {(1, 4), (6, 4), (2, 1)}


def test_no_leak_between_graph_and_labels():
    split = build_split(EDGES, COUNTRIES, SPLIT)
    graph = training_graph(EDGES, SPLIT[0])
    positives = split[split["label"] == 1]
    assert not any(graph.has_edge(src, dst) for src, dst in zip(positives["src"], positives["dst"]))
    # Features only depend on the past: dropping every future edge changes nothing.
    past_only = build_split(EDGES[EDGES["season"] <= SPLIT[0]], COUNTRIES, SPLIT)
    assert past_only.empty  # no label without the future...
    features = pair_features(training_graph(EDGES[EDGES["season"] <= SPLIT[0]], SPLIT[0]),
                             list(zip(split["src"], split["dst"])), COUNTRIES)
    pd.testing.assert_frame_equal(features, split.drop(columns="label"))  # ...but identical features


def test_negatives_count_seed_and_exclusions():
    split = build_split(EDGES, COUNTRIES, SPLIT, negatives_per_positive=5)
    negatives = split[split["label"] == 0]
    assert split["label"].sum() == 3
    assert len(negatives) == 15
    window = EDGES[EDGES["season"] <= 2023]
    traded = set(zip(window["src"], window["dst"]))
    assert not traded & set(zip(negatives["src"], negatives["dst"]))
    assert all(src != dst for src, dst in zip(negatives["src"], negatives["dst"]))
    pd.testing.assert_frame_equal(split, build_split(EDGES, COUNTRIES, SPLIT, negatives_per_positive=5))


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


@pytest.fixture(name="duckdb_file")
def duckdb_file_fixture(tmp_path):
    """A tiny DuckDB file with the three Transfermarkt tables the dataset reads."""
    path = tmp_path / "mini.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE competitions (competition_id VARCHAR, country_name VARCHAR)")
        con.execute("INSERT INTO competitions VALUES ('FR1', 'France'), ('GB1', 'England'), ('XX1', 'Elsewhere')")
        con.execute("CREATE TABLE clubs (club_id VARCHAR, domestic_competition_id VARCHAR)")
        con.execute("INSERT INTO clubs VALUES ('1', 'FR1'), ('2', 'FR1'), ('3', 'GB1'), ('99', 'XX1')")
        con.execute(
            "CREATE TABLE transfers (from_club_id INTEGER, to_club_id INTEGER, transfer_season VARCHAR)"
        )
        con.execute(
            "INSERT INTO transfers VALUES (1, 2, '21/22'), (1, 2, '21/22'), (2, 3, '22/23'), "
            "(3, 99, '22/23'), (1, 1, '22/23'), (2, 1, NULL), (500, 1, '22/23')"
        )
    return path


def test_load_edges_keeps_scope_and_aggregates(duckdb_file):
    edges = load_edges(duckdb_file, ["FR1", "GB1"])
    assert sorted(edges.itertuples(index=False, name=None)) == [(1, 2, 2021, 2), (2, 3, 2022, 1)]


def test_load_countries(duckdb_file):
    assert load_countries(duckdb_file, ["FR1", "GB1"]) == {1: "France", 2: "France", 3: "England"}
