"""Does a FastRP embedding of clubs help the link prediction? Owner: C.

Run with `python -m ml.fastrp_experiment` (a few minutes, reads DuckDB only).

The `embedding` written by `GdsService.refresh` cannot be used: it is computed on every season, the label windows
included, so it would leak the future. FastRP is recomputed here in numpy on each feature graph (same window as the
model), with the settings of `refresh` (iteration weights 0, 1, 1): sparse random vectors, then two rounds of
"average of the neighbours" weighted by transfer counts, each row L2-normalised.

Three ways to feed it to the model, each added to the five topological features:
- `cosine`: one feature, the cosine of the two (undirected) club embeddings;
- `hadamard`: 64 features, the element-wise product of the two undirected embeddings (usual link-prediction recipe);
- `directed`: 64 features, element-wise product of the seller's embedding on its *outgoing* transfers and the
  buyer's embedding on its *incoming* transfers (keeps the seller and buyer roles).
A variant is kept only if it raises the validation AUC-PR (none does: see `docs/ml_report.md`).
"""

from dataclasses import replace

import networkx as nx
import numpy as np
import pandas as pd
import scipy.sparse as sp
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from ml.dataset import (
    FEATURES,
    SEED,
    TEST_SPLIT,
    TRAIN_SPLIT,
    Universe,
    build_full_split,
    build_split,
    feature_graph,
    load_universe,
)
from ml.train_link_prediction import FEATURE_WINDOW, VALIDATION_SPLIT, evaluate

ITERATION_WEIGHTS = (0.0, 1.0, 1.0)
DIMENSION = 64
SEEDS = (1, 2, SEED)
VARIANTS = ("cosine", "hadamard", "directed")


def _normalise(matrix: np.ndarray) -> np.ndarray:
    """Rows scaled to unit L2 norm (zero rows stay zero)."""
    return matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)


def transition_matrix(graph: nx.DiGraph, index: dict[int, int], direction: str = "both") -> sp.csr_matrix:
    """Row-normalised adjacency weighted by transfer counts: each row averages its neighbours.

    `both`: undirected (counts of both directions added); `out`: a club averages the clubs it sold to;
    `in`: a club averages the clubs it bought from.
    """
    weights: dict[tuple[int, int], float] = {}
    for src, dst, data in graph.edges(data=True):
        pairs = {"both": [(src, dst), (dst, src)], "out": [(src, dst)], "in": [(dst, src)]}[direction]
        for row, col in pairs:
            key = (index[row], index[col])
            weights[key] = weights.get(key, 0.0) + data.get("count", 1)
    rows, cols = [key[0] for key in weights], [key[1] for key in weights]
    adjacency = sp.csr_matrix((list(weights.values()), (rows, cols)), shape=(len(index), len(index)))
    degree = np.asarray(adjacency.sum(axis=1)).ravel()
    return sp.diags(1.0 / np.where(degree == 0, 1.0, degree)) @ adjacency


def fastrp(
    graph: nx.DiGraph, dimension: int = DIMENSION, seed: int = SEED, direction: str = "both"
) -> dict[int, np.ndarray]:
    """FastRP embedding of every node of `graph` (see `transition_matrix` for `direction`)."""
    index = {node: position for position, node in enumerate(sorted(graph.nodes))}
    transition = transition_matrix(graph, index, direction)
    rng = np.random.default_rng(seed)
    # Very sparse random projection (Achlioptas): +-sqrt(3) with probability 1/6 each, 0 otherwise.
    current = _normalise(rng.choice([-1.0, 0.0, 1.0], size=(len(index), dimension), p=[1 / 6, 2 / 3, 1 / 6]))
    embedding = ITERATION_WEIGHTS[0] * current
    for weight in ITERATION_WEIGHTS[1:]:
        current = _normalise(transition @ current)
        embedding = embedding + weight * current
    return {node: embedding[position] for node, position in index.items()}


def _vectors(embedding: dict[int, np.ndarray], clubs, dimension: int) -> np.ndarray:
    """Embedding of each club, zeros for a club absent from the graph."""
    zero = np.zeros(dimension)
    return np.array([embedding.get(club, zero) for club in clubs])


def with_fastrp_cosine(frame: pd.DataFrame, graph: nx.DiGraph, dimension: int = DIMENSION, seed: int = SEED):
    """`frame` plus `fastrp_cosine`, the cosine of the two club embeddings computed on `graph` (0 if unknown)."""
    embedding = fastrp(graph, dimension, seed)
    left, right = _vectors(embedding, frame["src"], dimension), _vectors(embedding, frame["dst"], dimension)
    norms = np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1)
    frame = frame.copy()
    frame["fastrp_cosine"] = np.where(norms > 0, (left * right).sum(axis=1) / np.where(norms > 0, norms, 1), 0.0)
    return frame


def with_embedding_product(frame: pd.DataFrame, graph: nx.DiGraph, directed: bool, seed: int = SEED):
    """`frame` plus `emb_0` ... `emb_63`: element-wise product of the seller's and the buyer's embeddings."""
    if directed:
        seller, buyer = fastrp(graph, DIMENSION, seed, "out"), fastrp(graph, DIMENSION, seed, "in")
    else:
        seller = buyer = fastrp(graph, DIMENSION, seed)
    product = _vectors(seller, frame["src"], DIMENSION) * _vectors(buyer, frame["dst"], DIMENSION)
    columns = [f"emb_{position}" for position in range(DIMENSION)]
    return pd.concat([frame.reset_index(drop=True), pd.DataFrame(product, columns=columns)], axis=1)


def add_variant(frame: pd.DataFrame, graph: nx.DiGraph, variant: str, seed: int = SEED) -> tuple[pd.DataFrame, list]:
    """`frame` with the columns of `variant`, and the list of model inputs."""
    if variant == "cosine":
        return with_fastrp_cosine(frame, graph, seed=seed), FEATURES + ["fastrp_cosine"]
    extended = with_embedding_product(frame, graph, directed=variant == "directed", seed=seed)
    return extended, FEATURES + [f"emb_{position}" for position in range(DIMENSION)]


def models_for(columns: list[str]) -> dict:
    """The two models of `ml.train_link_prediction`, with log1p only on the count features."""
    extra = [column for column in columns if column not in FEATURES]
    scaling = ColumnTransformer(
        [("counts", make_pipeline(FunctionTransformer(np.log1p), StandardScaler()), FEATURES)]
        + ([("embedding", StandardScaler(), extra)] if extra else [])
    )
    return {
        "logistic_regression": make_pipeline(scaling, LogisticRegression(max_iter=2000)),
        "gradient_boosting": GradientBoostingClassifier(random_state=SEED),
    }


def validation_score(universe: Universe, variant: str | None, seed: int = SEED) -> float:
    """Validation AUC-PR of logistic regression, fit on the fit window, with `variant` (None = five features)."""
    fit, validation = build_split(universe, VALIDATION_SPLIT), build_full_split(universe, TRAIN_SPLIT)
    columns = FEATURES
    if variant:
        fit, columns = add_variant(fit, feature_graph(universe, VALIDATION_SPLIT[0]), variant, seed)
        validation, _ = add_variant(validation, feature_graph(universe, TRAIN_SPLIT[0]), variant, seed)
    model = models_for(columns)["logistic_regression"].fit(fit[columns], fit["label"])
    return float(average_precision_score(validation["label"], model.predict_proba(validation[columns])[:, 1]))


def test_metrics(universe: Universe, variant: str | None) -> dict[str, dict]:
    """Test metrics of both models fit on the train window, with `variant` (None = five features)."""
    train, test = build_split(universe, TRAIN_SPLIT), build_full_split(universe, TEST_SPLIT)
    columns = FEATURES
    if variant:
        train, columns = add_variant(train, feature_graph(universe, TRAIN_SPLIT[0]), variant)
        test, _ = add_variant(test, feature_graph(universe, TEST_SPLIT[0]), variant)
    return {
        name: evaluate(test["label"], model.fit(train[columns], train["label"]).predict_proba(test[columns])[:, 1])
        for name, model in models_for(columns).items()
    }


def main() -> None:
    """Validation AUC-PR of each variant over three seeds, then test metrics at seed 42."""
    universe = replace(load_universe(), feature_window=FEATURE_WINDOW)
    print(f"validation AUC-PR without FastRP: {validation_score(universe, None):.4f}")
    for variant in VARIANTS:
        scores = [validation_score(universe, variant, seed) for seed in SEEDS]
        print(f"validation AUC-PR, {variant}: " + ", ".join(f"{score:.4f}" for score in scores))
    for variant in (None, *VARIANTS):
        for name, metrics in test_metrics(universe, variant).items():
            print(f"test, {variant or 'none'}, {name}:", {key: round(value, 3) for key, value in metrics.items()})


if __name__ == "__main__":
    main()
