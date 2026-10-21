"""ml.fastrp_experiment on small graphs (no database). Owner: C."""

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from ml.fastrp_experiment import fastrp, with_embedding_product, with_fastrp_cosine


def two_triangles() -> nx.DiGraph:
    """Two dense groups of clubs joined by one weak edge, plus an isolated club."""
    graph = nx.DiGraph()
    graph.add_weighted_edges_from(
        [(1, 2, 3), (2, 3, 3), (3, 1, 3), (4, 5, 3), (5, 6, 3), (6, 4, 3), (3, 4, 1)], weight="count"
    )
    graph.add_node(7)
    return graph


def test_embeddings_are_reproducible_and_unit_norm():
    first, second = fastrp(two_triangles(), dimension=16, seed=3), fastrp(two_triangles(), dimension=16, seed=3)
    assert set(first) == set(range(1, 8))
    assert all(np.allclose(first[node], second[node]) for node in first)
    # Weights [0, 1, 1]: the sum of two unit vectors, so the norm is between 0 and 2; an isolated club gets 0.
    assert all(0 < np.linalg.norm(first[node]) <= 2 + 1e-9 for node in range(1, 7))
    assert np.linalg.norm(first[7]) == 0


def test_same_group_is_closer_than_the_other_group():
    frame = pd.DataFrame({"src": [1, 1, 1, 9], "dst": [2, 5, 7, 1]})
    cosine = with_fastrp_cosine(frame, two_triangles(), dimension=64, seed=42)["fastrp_cosine"].tolist()
    assert cosine[0] > cosine[1]  # 1 and 2 share neighbours, 1 and 5 do not
    assert cosine[2] == 0.0  # isolated club: zero vector
    assert cosine[3] == 0.0  # club unknown to the graph
    assert -1 - 1e-9 <= min(cosine) and max(cosine) <= 1 + 1e-9


def test_direction_does_not_matter():
    graph = two_triangles()
    reversed_graph = graph.reverse(copy=True)
    left, right = fastrp(graph, 8, seed=1), fastrp(reversed_graph, 8, seed=1)
    assert all(left[node] == pytest.approx(right[node]) for node in graph)


def test_directed_embeddings_follow_the_roles():
    # Club 1 only sells (to 2 and 3); club 4 only buys (from 2 and 3).
    graph = nx.DiGraph()
    graph.add_weighted_edges_from([(1, 2, 1), (1, 3, 1), (2, 4, 1), (3, 4, 1)], weight="count")
    outgoing, incoming = fastrp(graph, 16, seed=5, direction="out"), fastrp(graph, 16, seed=5, direction="in")
    assert np.linalg.norm(outgoing[4]) == 0  # never sold: nothing to average
    assert np.linalg.norm(incoming[1]) == 0  # never bought
    assert np.linalg.norm(outgoing[1]) > 0 and np.linalg.norm(incoming[4]) > 0


def test_embedding_product_columns():
    frame = pd.DataFrame({"src": [1, 9], "dst": [2, 1], "label": [1, 0]})
    extended = with_embedding_product(frame, two_triangles(), directed=False, seed=1)
    assert [column for column in extended.columns if column.startswith("emb_")] == [f"emb_{i}" for i in range(64)]
    assert (extended.loc[1, [f"emb_{i}" for i in range(64)]] == 0).all()  # unknown club 9: zero product
    directed = with_embedding_product(frame, two_triangles(), directed=True, seed=1)
    assert len(directed) == 2 and directed["label"].tolist() == [1, 0]
