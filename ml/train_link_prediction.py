"""Train and evaluate the club-to-club link-prediction model. Owner: C.

Run with `python -m ml.train_link_prediction` (`--no-write` to skip Neo4j).

Protocol (details in `docs/ml_report.md`):
1. Model selection, temporal too: fit each model on the sampled window "graph up to 2018, new pairs of 2019-2020",
   validate on the full window "graph up to 2020, new pairs of 2021-2022"; keep the best AUC-PR. A random k-fold
   on one window measures interpolation, not the step to future seasons (it preferred the model that is worse later).
2. Fit each model on the sampled train split (graph up to 2020, new pairs of 2021-2022, 5 negatives per positive).
3. Evaluate once on the full test split: every open pair of scope clubs at the end of 2022, labelled by the new
   pairs of 2023-2024 (about 3.5 % positives). Baseline: common neighbours alone.
4. Refit the chosen model on the sampled test split (the most recent labelled window), score every open pair of the
   full graph, correct the probabilities for the 1-in-6 sampling, and write the predictions to Neo4j:
   `PREDICTED_LINK` for the best destinations of each scope club, `PREDICTED_MOVE` from each player of that club.
"""

import argparse
import json
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from app.config import get_settings
from app.database.neo4j import Neo4jClient
from ml.dataset import (
    FEATURES,
    SEED,
    TEST_SPLIT,
    TRAIN_SPLIT,
    Universe,
    build_full_split,
    build_split,
    load_universe,
    open_pairs,
    pair_features,
    training_graph,
)

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
VERSION_PREFIX = "lp-"
BASELINE = "common_neighbours"
VALIDATION_SPLIT = (2018, (2019, 2020))  # fit window of the model selection
TOP_K = (50, 100)
LINKS_PER_CLUB = 5
MOVES_PER_PLAYER = 3
BATCH_SIZE = 5000
WRITE_TIMEOUT_S = 600.0  # writing thousands of relationships can exceed the 10 s limit of the API

DELETE_OLD = """
MATCH ()-[r:PREDICTED_LINK|PREDICTED_MOVE]->()
WHERE r.model_version STARTS WITH $prefix AND r.model_version <> $version
WITH r LIMIT 10000
DELETE r
RETURN count(*) AS deleted
"""

# MATCH, never MERGE, on clubs: a pair whose clubs are not in the graph is silently skipped.
WRITE_LINKS = """
UNWIND $rows AS row
MATCH (src:Club {club_id: row.src})
MATCH (dst:Club {club_id: row.dst})
MERGE (src)-[r:PREDICTED_LINK {model_version: $version}]->(dst)
SET r.prob = row.prob
RETURN count(r) AS written
"""

# Probability of a move = probability of the pair (current club, destination): a pair-level score, not a
# player-level model (documented in docs/ml_report.md).
WRITE_MOVES = """
UNWIND $clubs AS club_id
MATCH (src:Club {club_id: club_id})<-[:PLAYS_FOR]-(p:Player)
MATCH (src)-[l:PREDICTED_LINK {model_version: $version}]->(dst:Club)
WITH p, dst, l.prob AS prob
ORDER BY prob DESC, dst.club_id
WITH p, collect({club: dst, prob: prob})[..$per_player] AS top
UNWIND top AS move
WITH p, move.club AS dst, move.prob AS prob
MERGE (p)-[m:PREDICTED_MOVE {model_version: $version}]->(dst)
SET m.prob = prob
RETURN count(m) AS written
"""


def make_models() -> dict:
    """The two candidate models (log1p because counts and degree products are heavy-tailed)."""
    return {
        "logistic_regression": make_pipeline(
            FunctionTransformer(np.log1p), StandardScaler(), LogisticRegression(max_iter=1000)
        ),
        "gradient_boosting": GradientBoostingClassifier(random_state=SEED),
    }


def precision_at_k(labels, scores, k: int) -> float:
    """Share of positives among the `k` best scores (ties kept in input order, stable sort)."""
    order = np.argsort(-np.asarray(scores, dtype=float), kind="stable")[:k]
    return float(np.asarray(labels)[order].mean()) if len(order) else 0.0


def evaluate(labels, scores) -> dict[str, float]:
    """AUC-PR, ROC AUC and precision at 50 and 100."""
    metrics = {
        "auc_pr": float(average_precision_score(labels, scores)),
        "roc_auc": float(roc_auc_score(labels, scores)),
    }
    metrics.update({f"precision_at_{k}": precision_at_k(labels, scores, k) for k in TOP_K})
    return metrics


def adjust_prior(prob, sample_rate: float, true_rate: float):
    """Correct probabilities learnt at prevalence `sample_rate` to the real prevalence `true_rate` (odds ratio)."""
    prob = np.clip(np.asarray(prob, dtype=float), 1e-12, 1 - 1e-12)
    factor = (true_rate / (1 - true_rate)) / (sample_rate / (1 - sample_rate))
    odds = prob / (1 - prob) * factor
    return odds / (1 + odds)


def compare(splits: dict[str, pd.DataFrame]) -> tuple[dict[str, dict], str]:
    """Metrics of the baseline and of each model on the full test split, and the model chosen on validation.

    `splits` holds `fit` and `validation` (model selection), `train` and `test_full` (final evaluation).
    """
    test_full, validation = splits["test_full"], splits["validation"]
    results = {
        "common_neighbours_baseline": {
            "val_auc_pr": float(average_precision_score(validation["label"], validation[BASELINE])),
            **evaluate(test_full["label"], test_full[BASELINE]),
        }
    }
    for name, model in make_models().items():
        selector = clone(model).fit(splits["fit"][FEATURES], splits["fit"]["label"])
        val_auc_pr = average_precision_score(validation["label"], selector.predict_proba(validation[FEATURES])[:, 1])
        fitted = clone(model).fit(splits["train"][FEATURES], splits["train"]["label"])
        metrics = evaluate(test_full["label"], fitted.predict_proba(test_full[FEATURES])[:, 1])
        results[name] = {"val_auc_pr": float(val_auc_pr), **metrics}
    chosen = max(make_models(), key=lambda name: results[name]["val_auc_pr"])
    results["random"] = {"auc_pr": float(test_full["label"].mean())}  # AUC-PR of a random ranking = prevalence
    return results, chosen


def score_open_pairs(model, universe: Universe, sample_rate: float, true_rate: float) -> pd.DataFrame:
    """Probability of every open pair of scope clubs on the graph of every known season."""
    graph = training_graph(universe.edges, int(universe.edges["season"].max()))
    frame = pair_features(graph, open_pairs(graph, universe.clubs), universe.countries)
    frame["prob"] = adjust_prior(model.predict_proba(frame[FEATURES])[:, 1], sample_rate, true_rate)
    return frame


def top_per_source(scored: pd.DataFrame, per_source: int = LINKS_PER_CLUB) -> list[dict]:
    """The `per_source` most probable destinations of each selling club, as rows for Neo4j."""
    best = scored.sort_values(["src", "prob", "dst"], ascending=[True, False, True]).groupby("src").head(per_source)
    return [
        {"src": int(row.src), "dst": int(row.dst), "prob": round(float(row.prob), 4)}
        for row in best.itertuples(index=False)
    ]


def write_predictions(
    client: Neo4jClient, links: list[dict], version: str, moves_per_player: int = MOVES_PER_PLAYER
) -> dict[str, int]:
    """Replace older `lp-` predictions by `links` and the player moves derived from them."""
    deleted = 1
    while deleted:
        deleted = client.write(DELETE_OLD, {"prefix": VERSION_PREFIX, "version": version})[0]["deleted"]
    written_links = sum(
        client.write(WRITE_LINKS, {"rows": links[start : start + BATCH_SIZE], "version": version})[0]["written"]
        for start in range(0, len(links), BATCH_SIZE)
    )
    clubs = sorted({row["src"] for row in links})
    written_moves = sum(
        client.write(
            WRITE_MOVES, {"clubs": clubs[start : start + 50], "version": version, "per_player": moves_per_player}
        )[0]["written"]
        for start in range(0, len(clubs), 50)
    )
    return {"predicted_links": written_links, "predicted_moves": written_moves}


def markdown_table(results: dict[str, dict]) -> str:
    """Metrics as the Markdown table of docs/ml_report.md."""
    columns = ["val_auc_pr", "auc_pr", "roc_auc", "precision_at_50", "precision_at_100"]
    lines = ["| Model | " + " | ".join(columns) + " |", "| --- |" + " --- |" * len(columns)]
    for name, metrics in results.items():
        cells = [f"{metrics[column]:.3f}" if column in metrics else "-" for column in columns]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def final_model(universe: Universe, chosen: str) -> tuple[object, list[dict], dict]:
    """Refit `chosen` on the most recent labelled window and score every open pair of the current graph."""
    test, test_full = build_split(universe, TEST_SPLIT), build_full_split(universe, TEST_SPLIT)
    model = make_models()[chosen].fit(test[FEATURES], test["label"])
    rates = {"sample_rate": float(test["label"].mean()), "true_rate": float(test_full["label"].mean())}
    links = top_per_source(score_open_pairs(model, universe, rates["sample_rate"], rates["true_rate"]))
    return model, links, rates


def main(argv: list[str] | None = None) -> None:
    """Entry point of the training."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-write", action="store_true", help="train and evaluate only, do not touch Neo4j")
    args = parser.parse_args(argv)

    universe = load_universe()
    splits = {
        "fit": build_split(universe, VALIDATION_SPLIT),
        "validation": build_full_split(universe, TRAIN_SPLIT),
        "train": build_split(universe, TRAIN_SPLIT),
        "test_full": build_full_split(universe, TEST_SPLIT),
    }
    results, chosen = compare(splits)
    print(markdown_table(results))
    print(f"chosen on temporal validation: {chosen}")

    version = f"{VERSION_PREFIX}{chosen}-{date.today():%Y%m%d}"
    model, links, rates = final_model(universe, chosen)
    ARTIFACTS.mkdir(exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES, "version": version}, ARTIFACTS / "link_prediction.joblib")
    summary = {"version": version, "chosen": chosen, **rates, "predicted_links": len(links), "results": results}
    (ARTIFACTS / "metrics.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if args.no_write:
        return
    settings = get_settings()
    client = Neo4jClient(
        settings.neo4j_uri, (settings.neo4j_user, settings.neo4j_password), settings.neo4j_database, WRITE_TIMEOUT_S
    )
    try:
        print(write_predictions(client, links, version))
    finally:
        client.close()


if __name__ == "__main__":
    main()
