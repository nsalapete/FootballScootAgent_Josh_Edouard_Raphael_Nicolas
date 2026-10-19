"""Temporal link-prediction dataset on the club transfer network, read straight from DuckDB. Owner: C.

A sample is a directed pair of clubs (seller, buyer). The question is: will a pair that never traded before
the cut-off season trade during the target seasons?

- Edges: one row per (seller, buyer, season) with the number of transfers, both clubs in the scope competitions.
- Graph: every edge up to `train_until` (included). Features are computed on this graph only.
- Positives: pairs that trade during `target_seasons` and never traded up to `train_until`, both clubs
  present in the graph (a club unseen before has no neighbourhood to learn from).
- Negatives: random pairs of graph clubs that did not trade up to `target_seasons`, `negatives_per_positive`
  per positive, fixed seed.

No leakage: nothing after `train_until` is used to compute a feature; later seasons only provide labels.
"""

import math
import random
from pathlib import Path

import duckdb
import networkx as nx
import pandas as pd

from app.config import get_settings

FEATURES = ["common_neighbours", "adamic_adar", "preferential_attachment", "same_country", "pagerank_gap"]
SEED = 42
NEGATIVES_PER_POSITIVE = 5
TEST_SPLIT = (2022, (2023, 2024))  # graph up to 2022, new pairs of 2023 and 2024
TRAIN_SPLIT = (2020, (2021, 2022))  # same protocol two seasons earlier, to fit the model

EDGES_SQL = """
SELECT t.from_club_id AS src, t.to_club_id AS dst, t.transfer_season AS season_label, count(*) AS count
FROM transfers t
JOIN clubs a ON a.club_id = CAST(t.from_club_id AS VARCHAR)
JOIN clubs b ON b.club_id = CAST(t.to_club_id AS VARCHAR)
WHERE a.domestic_competition_id IN (SELECT unnest($competitions))
  AND b.domestic_competition_id IN (SELECT unnest($competitions))
  AND t.from_club_id <> t.to_club_id
  AND t.transfer_season IS NOT NULL
GROUP BY ALL
"""

COUNTRIES_SQL = """
SELECT CAST(c.club_id AS INTEGER) AS club_id, k.country_name AS country
FROM clubs c
JOIN competitions k ON k.competition_id = c.domestic_competition_id
WHERE c.domestic_competition_id IN (SELECT unnest($competitions))
"""


def parse_season(label: str) -> int:
    """Transfermarkt season label to its start year: '22/23' -> 2022, '99/00' -> 1999."""
    start = int(label.split("/")[0])
    return start + (1900 if start >= 50 else 2000)


def load_edges(duckdb_path: str | Path | None = None, competitions: list[str] | None = None) -> pd.DataFrame:
    """Club-to-club transfer counts per season (columns src, dst, season, count)."""
    settings = get_settings()
    with duckdb.connect(str(duckdb_path or settings.duckdb_path), read_only=True) as con:
        frame = con.execute(EDGES_SQL, {"competitions": competitions or settings.competitions}).df()
    frame["season"] = frame.pop("season_label").map(parse_season)
    frame = frame.groupby(["src", "dst", "season"], as_index=False)["count"].sum()
    return frame.astype({"src": int, "dst": int, "season": int, "count": int})


def load_countries(duckdb_path: str | Path | None = None, competitions: list[str] | None = None) -> dict[int, str]:
    """Country of each club in scope, from its domestic competition."""
    settings = get_settings()
    with duckdb.connect(str(duckdb_path or settings.duckdb_path), read_only=True) as con:
        frame = con.execute(COUNTRIES_SQL, {"competitions": competitions or settings.competitions}).df()
    return dict(zip(frame["club_id"].astype(int), frame["country"]))


def training_graph(edges: pd.DataFrame, until: int) -> nx.DiGraph:
    """Directed graph of every transfer up to season `until` (included), weighted by count."""
    graph = nx.DiGraph()
    past = edges[edges["season"] <= until].groupby(["src", "dst"], as_index=False)["count"].sum()
    graph.add_weighted_edges_from(past.itertuples(index=False, name=None), weight="count")
    return graph


def new_pairs(edges: pd.DataFrame, graph: nx.DiGraph, seasons: tuple[int, ...]) -> set[tuple[int, int]]:
    """Pairs trading during `seasons`, absent from `graph`, with both clubs in `graph`."""
    target = edges[edges["season"].isin(seasons)]
    pairs = set(zip(target["src"], target["dst"]))
    return {(src, dst) for src, dst in pairs if src in graph and dst in graph and not graph.has_edge(src, dst)}


def sample_negatives(graph: nx.DiGraph, excluded: set[tuple[int, int]], count: int, seed: int = SEED) -> list:
    """`count` distinct directed pairs of graph clubs, not edges of `graph` and not in `excluded`."""
    nodes = sorted(graph.nodes)
    blocked = set(graph.edges) | {(src, dst) for src, dst in excluded if src != dst and src in graph and dst in graph}
    count = min(count, len(nodes) * (len(nodes) - 1) - len(blocked))
    rng = random.Random(seed)
    chosen: set[tuple[int, int]] = set()
    while len(chosen) < count:
        src, dst = rng.choice(nodes), rng.choice(nodes)
        pair = (src, dst)
        if src != dst and pair not in blocked and pair not in chosen:
            chosen.add(pair)
    return sorted(chosen)


def pair_features(graph: nx.DiGraph, pairs: list[tuple[int, int]], countries: dict[int, str]) -> pd.DataFrame:
    """Topological features of each pair, computed on `graph` only (no future information)."""
    undirected = graph.to_undirected()
    pagerank = nx.pagerank(graph, weight="count") if graph.number_of_nodes() else {}
    rows = []
    for src, dst in pairs:
        known = src in graph and dst in graph
        common = list(nx.common_neighbors(undirected, src, dst)) if known else []
        rows.append(
            {
                "src": src,
                "dst": dst,
                "common_neighbours": len(common),
                # A common neighbour touches both clubs, so its degree is at least 2 and log(degree) > 0.
                "adamic_adar": sum(1.0 / math.log(undirected.degree(node)) for node in common),
                "preferential_attachment": undirected.degree(src) * undirected.degree(dst) if known else 0,
                "same_country": int(src in countries and countries.get(src) == countries.get(dst)),
                "pagerank_gap": abs(pagerank.get(src, 0.0) - pagerank.get(dst, 0.0)),
            }
        )
    return pd.DataFrame(rows, columns=["src", "dst", *FEATURES])


def build_split(
    edges: pd.DataFrame,
    countries: dict[int, str],
    split: tuple[int, tuple[int, ...]],
    *,
    negatives_per_positive: int = NEGATIVES_PER_POSITIVE,
    seed: int = SEED,
) -> pd.DataFrame:
    """Labelled pairs for `split = (train_until, target_seasons)`: 1 = new link during the target seasons."""
    train_until, target_seasons = split
    graph = training_graph(edges, train_until)
    positives = new_pairs(edges, graph, target_seasons)
    # A negative must not trade at any point up to the end of the target window either.
    window = edges[edges["season"] <= max(target_seasons)]
    excluded = set(zip(window["src"], window["dst"]))
    negatives = sample_negatives(graph, excluded, negatives_per_positive * len(positives), seed)
    pairs = sorted(positives) + negatives
    frame = pair_features(graph, pairs, countries)
    frame["label"] = [1] * len(positives) + [0] * len(negatives)
    return frame


def load_splits(duckdb_path: str | Path | None = None, competitions: list[str] | None = None):
    """(train, test) frames: train = graph up to 2020 and new pairs of 2021-2022, test = the official split."""
    edges = load_edges(duckdb_path, competitions)
    countries = load_countries(duckdb_path, competitions)
    train = build_split(edges, countries, TRAIN_SPLIT)
    test = build_split(edges, countries, TEST_SPLIT)
    return train, test
