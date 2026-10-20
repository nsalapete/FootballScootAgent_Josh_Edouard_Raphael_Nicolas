"""ml.fastrp_experiment on small graphs (no database). Owner: C."""

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from ml.fastrp_experiment import fastrp, with_fastrp_cosine


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
