"""ml.train_link_prediction: metrics, probability correction, selection and Neo4j write-back. Owner: C."""

import numpy as np
import pandas as pd
import pytest

from ml.dataset import FEATURES
from ml.train_link_prediction import (
    adjust_prior,
    compare,
    evaluate,
    markdown_table,
    precision_at_k,
    top_per_source,
    write_predictions,
)


def test_precision_at_k():
    labels = [1, 0, 1, 0, 0]
    scores = [0.9, 0.8, 0.7, 0.1, 0.2]
    assert precision_at_k(labels, scores, 1) == 1.0
    assert precision_at_k(labels, scores, 2) == 0.5
    assert precision_at_k(labels, scores, 3) == pytest.approx(2 / 3)
    assert precision_at_k([0, 1], [0.5, 0.5], 1) == 0.0  # ties keep the input order


def test_evaluate_keys_and_perfect_ranking():
    metrics = evaluate([0, 1, 0, 1], [0.1, 0.9, 0.2, 0.8])
    assert set(metrics) == {"auc_pr", "roc_auc", "precision_at_50", "precision_at_100"}
    assert metrics["auc_pr"] == metrics["roc_auc"] == 1.0


def test_adjust_prior():
    prob = np.array([0.1, 0.5, 0.9])
    assert adjust_prior(prob, 0.2, 0.2) == pytest.approx(prob)
    adjusted = adjust_prior(prob, 1 / 6, 0.036)
    assert all(adjusted < prob) and list(adjusted) == sorted(adjusted)  # lower, same order
    # Odds of 0.5 at prevalence 1/6 (odds 1/5) become 0.036 / 0.964 * 5.
    assert adjusted[1] == pytest.approx(0.18 / 1.144, rel=1e-3)


def test_top_per_source():
    scored = pd.DataFrame(
        [(1, 2, 0.1), (1, 3, 0.5), (1, 4, 0.3), (2, 1, 0.2), (2, 3, 0.2)], columns=["src", "dst", "prob"]
    )
    assert top_per_source(scored, 2) == [
        {"src": 1, "dst": 3, "prob": 0.5},
        {"src": 1, "dst": 4, "prob": 0.3},
        {"src": 2, "dst": 1, "prob": 0.2},  # tie on prob: smaller destination id first
        {"src": 2, "dst": 3, "prob": 0.2},
    ]


def synthetic(rows: int, seed: int) -> pd.DataFrame:
    """Pairs whose label depends on common neighbours, like the real data."""
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(rng.poisson(3, size=(rows, len(FEATURES))).astype(float), columns=FEATURES)
    frame["same_country"] = rng.integers(0, 2, rows)
    frame["label"] = (frame["common_neighbours"] + rng.normal(0, 1, rows) > 4).astype(int)
    return frame


def test_compare_reports_every_model_and_the_baseline():
    splits = {"fit": synthetic(300, 1), "validation": synthetic(300, 2), "train": synthetic(300, 3)}
    splits["test_full"] = synthetic(400, 4)
    results, chosen = compare(splits)
    assert chosen in ("logistic_regression", "gradient_boosting")
    assert set(results) == {"common_neighbours_baseline", "logistic_regression", "gradient_boosting", "random"}
    assert results["random"]["auc_pr"] == pytest.approx(splits["test_full"]["label"].mean())
    assert all(results[name]["auc_pr"] > results["random"]["auc_pr"] for name in ("logistic_regression",))
    table = markdown_table(results)
    assert table.splitlines()[0].startswith("| Model | val_auc_pr")
    assert "| random | - |" in table


# ---------- write-back on the test database (own nodes, negative ids, removed at the end) ----------

CLUBS = [-9301, -9302, -9303]
PLAYERS = [-9401, -9402]
CREATE_CLUBS = "UNWIND $clubs AS id CREATE (:Club {club_id: id, name: 'test-c-club ' + toString(id)})"
CREATE_PLAYERS = """
MATCH (c:Club {club_id: $club})
UNWIND $players AS id
CREATE (:Player {player_id: id, name: 'test-c-player ' + toString(id)})-[:PLAYS_FOR]->(c)
"""
MOVES = """
MATCH (p:Player)-[m:PREDICTED_MOVE]->(c:Club) WHERE p.player_id IN $players
RETURN p.player_id AS player, c.club_id AS club, m.prob AS prob, m.model_version AS version ORDER BY player
"""


@pytest.fixture(name="own_nodes")
def own_nodes_fixture(neo4j_db):
    """Three clubs and two players of role C; both players play for the first club."""
    neo4j_db.write(CREATE_CLUBS, {"clubs": CLUBS})
    neo4j_db.write(CREATE_PLAYERS, {"club": CLUBS[0], "players": PLAYERS})
    yield neo4j_db
    neo4j_db.write("MATCH (n:Club|Player) WHERE n.club_id IN $clubs OR n.player_id IN $players DETACH DELETE n",
                   {"clubs": CLUBS, "players": PLAYERS})


@pytest.mark.neo4j
def test_write_predictions_matches_existing_clubs_only(own_nodes):
    links = [
        {"src": -9301, "dst": -9302, "prob": 0.3},
        {"src": -9301, "dst": -9303, "prob": 0.2},
        {"src": -9301, "dst": -9999, "prob": 0.9},  # unknown club: skipped, never created
    ]
    counts = write_predictions(own_nodes, links, "lp-test-c-1", moves_per_player=1)
    assert counts == {"predicted_links": 2, "predicted_moves": 2}
    assert not own_nodes.read("MATCH (c:Club {club_id: -9999}) RETURN c LIMIT 1")
    moves = own_nodes.read(MOVES, {"players": PLAYERS})
    assert moves == [
        {"player": -9402, "club": -9302, "prob": 0.3, "version": "lp-test-c-1"},
        {"player": -9401, "club": -9302, "prob": 0.3, "version": "lp-test-c-1"},
    ]


@pytest.mark.neo4j
def test_new_version_replaces_the_old_one_but_not_the_fixture(own_nodes):
    write_predictions(own_nodes, [{"src": -9301, "dst": -9302, "prob": 0.3}], "lp-test-c-1")
    write_predictions(own_nodes, [{"src": -9301, "dst": -9303, "prob": 0.1}], "lp-test-c-2")
    versions = own_nodes.read(
        "MATCH ()-[r:PREDICTED_LINK|PREDICTED_MOVE]->() RETURN DISTINCT r.model_version AS version ORDER BY version"
    )
    assert [row["version"] for row in versions] == ["fixture", "lp-test-c-2"]
