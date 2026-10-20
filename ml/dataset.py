"""Temporal link-prediction dataset on the club transfer network, read straight from DuckDB. Owner: C.

A sample is a directed pair of scope clubs (seller, buyer). The question is: will a pair that never traded before
the cut-off season trade during the target seasons?

The universe is the one of the Neo4j graph, built with the ingestion code of A (`scripts/seed_data.py`):
- scope clubs: clubs that played a league game of `SCOPE_COMPETITIONS` from `SCOPE_FROM_SEASON` on;
- transfers: those with a scope club on at least one side, cleaned by `scripts/data_quality.py` (loans, duplicates,
  placeholders, future dates, seasons before the scope), exactly the transfers behind `SENDS_TO`.

- Edges: one row per (seller, buyer, season) with the number of transfers (light clubs included, like `SENDS_TO`).
- Graph: every edge up to `train_until` (included). Features are computed on this graph only.
- Positives: pairs of scope clubs that trade during `target_seasons` and never traded up to `train_until`, both
  clubs present in the graph (a club unseen before has no neighbourhood to learn from).
- Negatives: random pairs of scope clubs of the graph that did not trade up to `target_seasons`,
  `negatives_per_positive` per positive, fixed seed.

No leakage: nothing after `train_until` is used to compute a feature; later seasons only provide labels.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import networkx as nx
import pandas as pd

from app.config import get_settings
from scripts.data_quality import clean_transfers
from scripts.seed_data import (
    SCOPE_CLUBS_SQL,
    SCOPE_GAMES_SQL,
    SCOPE_PLAYS_IN_SQL,
    TRANSFERS_SQL,
    fetch,
    shape_transfer,
)

FEATURES = ["common_neighbours", "adamic_adar", "preferential_attachment", "same_country", "pagerank_gap"]
SEED = 42
NEGATIVES_PER_POSITIVE = 5
TEST_SPLIT = (2022, (2023, 2024))  # graph up to 2022, new pairs of 2023 and 2024
TRAIN_SPLIT = (2020, (2021, 2022))  # same protocol two seasons earlier, to fit the model
EDGE_COLUMNS = ["src", "dst", "season", "count"]

# Country of a scope club: the one of the competition it played in during its latest scope season.
COUNTRIES_SQL = """
SELECT p.club_id, k.country_name AS country
FROM (SELECT club_id, arg_max(competition_id, season) AS competition_id FROM scope_plays_in GROUP BY club_id) p
JOIN competitions k ON k.competition_id = p.competition_id
"""


@dataclass
class Universe:
    """Transfer edges per season, scope clubs and their country: the inputs of every split."""

    edges: pd.DataFrame
    clubs: frozenset[int] = field(default_factory=frozenset)
    countries: dict[int, str] = field(default_factory=dict)


def edges_from_transfers(rows: list[dict]) -> pd.DataFrame:
    """Cleaned transfer rows to transfer counts per (src, dst, season)."""
    frame = pd.DataFrame(
        [(row["from_club_id"], row["to_club_id"], row["season"]) for row in rows], columns=EDGE_COLUMNS[:3]
    )
    frame = frame.groupby(EDGE_COLUMNS[:3], as_index=False).size().rename(columns={"size": "count"})
    return frame.astype({column: int for column in EDGE_COLUMNS})[EDGE_COLUMNS]


def load_universe(
    duckdb_path: str | Path | None = None,
    competitions: list[str] | None = None,
    from_season: int | None = None,
    today: date | None = None,
) -> Universe:
    """Scope clubs, their country and the cleaned transfers, with the same rules as the ingestion."""
    settings = get_settings()
    scope = {
        "competitions": competitions or settings.competitions,
        "from_season": settings.scope_from_season if from_season is None else from_season,
    }
    with duckdb.connect(str(duckdb_path or settings.duckdb_path), read_only=True) as con:
        con.execute(SCOPE_GAMES_SQL, scope)
        con.execute(SCOPE_PLAYS_IN_SQL)
        con.execute(SCOPE_CLUBS_SQL)
        clubs = frozenset(int(row["club_id"]) for row in fetch(con, "SELECT club_id FROM scope_clubs"))
        countries = {int(row["club_id"]): row["country"] for row in fetch(con, COUNTRIES_SQL)}
        raw = fetch(con, TRANSFERS_SQL)
    cleaned = clean_transfers([shape_transfer(item) for item in raw], scope["from_season"], today or date.today())
    return Universe(edges=edges_from_transfers(cleaned.rows), clubs=clubs, countries=countries)


def training_graph(edges: pd.DataFrame, until: int) -> nx.DiGraph:
    """Directed graph of every transfer up to season `until` (included), weighted by count."""
    graph = nx.DiGraph()
    past = edges[edges["season"] <= until].groupby(["src", "dst"], as_index=False)["count"].sum()
    graph.add_weighted_edges_from(past.itertuples(index=False, name=None), weight="count")
    return graph


def candidate_clubs(graph: nx.DiGraph, clubs: frozenset[int]) -> set[int]:
    """Clubs a pair may involve: scope clubs present in `graph` (every graph club when `clubs` is empty)."""
    return set(graph.nodes) & clubs if clubs else set(graph.nodes)


def new_pairs(
    edges: pd.DataFrame, graph: nx.DiGraph, seasons: tuple[int, ...], clubs: frozenset[int] = frozenset()
) -> set[tuple[int, int]]:
    """Pairs of candidate clubs trading during `seasons` and absent from `graph`."""
    target = edges[edges["season"].isin(seasons)]
    candidates = candidate_clubs(graph, clubs)
    pairs = set(zip(target["src"], target["dst"]))
    return {
        (src, dst) for src, dst in pairs if src in candidates and dst in candidates and not graph.has_edge(src, dst)
    }


def sample_negatives(
    graph: nx.DiGraph, excluded: set[tuple[int, int]], count: int, seed: int = SEED, clubs: frozenset[int] = frozenset()
) -> list:
    """`count` distinct directed pairs of candidate clubs, not edges of `graph` and not in `excluded`."""
    candidates = candidate_clubs(graph, clubs)
    nodes = sorted(candidates)
    blocked = {
        (src, dst)
        for src, dst in set(graph.edges) | set(excluded)
        if src != dst and src in candidates and dst in candidates
    }
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
    universe: Universe,
    split: tuple[int, tuple[int, ...]],
    *,
    negatives_per_positive: int = NEGATIVES_PER_POSITIVE,
    seed: int = SEED,
) -> pd.DataFrame:
    """Labelled pairs for `split = (train_until, target_seasons)`: 1 = new link during the target seasons."""
    train_until, target_seasons = split
    edges = universe.edges
    graph = training_graph(edges, train_until)
    positives = new_pairs(edges, graph, target_seasons, universe.clubs)
    # A negative must not trade at any point up to the end of the target window either.
    window = edges[edges["season"] <= max(target_seasons)]
    excluded = set(zip(window["src"], window["dst"]))
    negatives = sample_negatives(graph, excluded, negatives_per_positive * len(positives), seed, universe.clubs)
    pairs = sorted(positives) + negatives
    frame = pair_features(graph, pairs, universe.countries)
    frame["label"] = [1] * len(positives) + [0] * len(negatives)
    return frame


def load_splits(universe: Universe | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(train, test) frames: train = graph up to 2020 and new pairs of 2021-2022, test = the official split."""
    universe = universe or load_universe()
    return build_split(universe, TRAIN_SPLIT), build_split(universe, TEST_SPLIT)
