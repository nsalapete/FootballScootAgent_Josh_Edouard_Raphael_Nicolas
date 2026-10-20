"""Does a FastRP embedding of clubs help the link prediction? Owner: C.

Run with `python -m ml.fastrp_experiment` (about 1 minute, reads DuckDB only).

The `embedding` written by `GdsService.refresh` cannot be used: it is computed on every season, the label windows
included, so it would leak the future. FastRP is recomputed here in numpy on each feature graph, with the settings
of `refresh` (iteration weights 0, 1, 1): sparse random vectors, then two rounds of "average of the neighbours"
(weighted by transfer counts, undirected), each row L2-normalised. The pair feature is the cosine similarity of the
two club embeddings. It is kept only if it raises the validation AUC-PR (it does not: see `docs/ml_report.md`).
"""

import numpy as np
import networkx as nx
import pandas as pd
import scipy.sparse as sp
from sklearn.metrics import average_precision_score

from ml.dataset import (
    FEATURES,
    SEED,
    TEST_SPLIT,
    TRAIN_SPLIT,
    build_full_split,
    build_split,
    load_universe,
    training_graph,
)
from ml.train_link_prediction import VALIDATION_SPLIT, evaluate, make_models

ITERATION_WEIGHTS = (0.0, 1.0, 1.0)
DIMENSIONS = (32, 64, 128)
SEEDS = (1, 2, SEED)


def _normalise(matrix: np.ndarray) -> np.ndarray:
    """Rows scaled to unit L2 norm (zero rows stay zero)."""
    return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)


def transition_matrix(graph: nx.DiGraph, index: dict[int, int]) -> sp.csr_matrix:
    """Row-normalised undirected adjacency: each row averages the neighbours, weighted by transfers both ways."""
    weights: dict[tuple[int, int], float] = {}
    for src, dst, data in graph.edges(data=True):
        key = (min(index[src], index[dst]), max(index[src], index[dst]))
        weights[key] = weights.get(key, 0.0) + data.get("count", 1)
    rows = [a for a, _ in weights] + [b for _, b in weights]
    cols = [b for _, b in weights] + [a for a, _ in weights]
    adjacency = sp.csr_matrix((list(weights.values()) * 2, (rows, cols)), shape=(len(index), len(index)))
    degree = np.asarray(adjacency.sum(axis=1)).ravel()
    return sp.diags(1.0 / np.where(degree == 0, 1.0, degree)) @ adjacency


def fastrp(graph: nx.DiGraph, dimension: int = 64, seed: int = SEED) -> dict[int, np.ndarray]:
    """FastRP embedding of every node of `graph`, undirected, weighted by `count` in both directions."""
    index = {node: position for position, node in enumerate(sorted(graph.nodes))}
    transition = transition_matrix(graph, index)
    rng = np.random.default_rng(seed)
    # Very sparse random projection (Achlioptas): +-sqrt(3) with probability 1/6 each, 0 otherwise.
    current = _normalise(rng.choice([-1.0, 0.0, 1.0], size=(len(index), dimension), p=[1 / 6, 2 / 3, 1 / 6]))
    embedding = ITERATION_WEIGHTS[0] * current
    for weight in ITERATION_WEIGHTS[1:]:
        current = _normalise(transition @ current)
        embedding = embedding + weight * current
    return {node: embedding[position] for node, position in index.items()}


def with_fastrp_cosine(frame: pd.DataFrame, graph: nx.DiGraph, dimension: int = 64, seed: int = SEED) -> pd.DataFrame:
    """`frame` plus `fastrp_cosine`, the cosine of the two club embeddings computed on `graph` (0 if unknown)."""
    embedding = fastrp(graph, dimension, seed)

    def cosine(src: int, dst: int) -> float:
        left, right = embedding.get(src), embedding.get(dst)
        if left is None or right is None:
            return 0.0
        norm = float(np.linalg.norm(left) * np.linalg.norm(right))
        return float(left @ right / norm) if norm else 0.0

    frame = frame.copy()
    frame["fastrp_cosine"] = [cosine(src, dst) for src, dst in zip(frame["src"], frame["dst"])]
    return frame


def validation_runs(universe, graphs: dict) -> None:
    """Validation AUC-PR of logistic regression without, then with, the feature for each size and seed."""
    fit, validation = build_split(universe, VALIDATION_SPLIT), build_full_split(universe, TRAIN_SPLIT)
    model = make_models()["logistic_regression"].fit(fit[FEATURES], fit["label"])
    baseline = average_precision_score(validation["label"], model.predict_proba(validation[FEATURES])[:, 1])
    print(f"validation AUC-PR without FastRP: {baseline:.4f}")
    columns = FEATURES + ["fastrp_cosine"]
    for dimension in DIMENSIONS:
        for seed in SEEDS:
            fit_x = with_fastrp_cosine(fit, graphs[VALIDATION_SPLIT], dimension, seed)
            val_x = with_fastrp_cosine(validation, graphs[TRAIN_SPLIT], dimension, seed)
            model = make_models()["logistic_regression"].fit(fit_x[columns], fit_x["label"])
            score = average_precision_score(val_x["label"], model.predict_proba(val_x[columns])[:, 1])
            print(f"validation AUC-PR with FastRP (dimension {dimension}, seed {seed}): {score:.4f}")


def final_test_runs(universe, graphs: dict) -> None:
    """Test metrics of both models without and with the feature (dimension 64, seed 42)."""
    train_x = with_fastrp_cosine(build_split(universe, TRAIN_SPLIT), graphs[TRAIN_SPLIT])
    test_x = with_fastrp_cosine(build_full_split(universe, TEST_SPLIT), graphs[TEST_SPLIT])
    print("feature alone on test:", round(average_precision_score(test_x["label"], test_x["fastrp_cosine"]), 4))
    for name in make_models():
        for label, features in (("without", FEATURES), ("with", FEATURES + ["fastrp_cosine"])):
            fitted = make_models()[name].fit(train_x[features], train_x["label"])
            metrics = evaluate(test_x["label"], fitted.predict_proba(test_x[features])[:, 1])
            print(name, label, "FastRP:", {key: round(value, 3) for key, value in metrics.items()})


def main() -> None:
    """Validation runs for several sizes and seeds, then the test metrics."""
    universe = load_universe()
    graphs = {split: training_graph(universe.edges, split[0]) for split in (VALIDATION_SPLIT, TRAIN_SPLIT, TEST_SPLIT)}
    validation_runs(universe, graphs)
    final_test_runs(universe, graphs)


if __name__ == "__main__":
    main()
