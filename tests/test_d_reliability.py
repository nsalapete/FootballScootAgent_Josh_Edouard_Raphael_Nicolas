"""Tests of the source reliability and of the nature labels of the hypothesis tools. Owner: D."""

import pytest

from app.agent.tools import ToolBox
from app.models.network import PredictedMove
from app.models.rumours import RumourOut
from app.services.rumours import DEFAULT_PRIOR, PRIOR_WEIGHT, RumourService, smoothed_reliability


def test_smoothing_formula():
    assert smoothed_reliability(0, 0, 0.5) == 0.5  # unknown source: the prior
    assert smoothed_reliability(1, 1, 0.5) == pytest.approx(3.5 / 6)  # one success is not proof
    assert smoothed_reliability(90, 100, 0.5) == pytest.approx((90 + PRIOR_WEIGHT * 0.5) / (100 + PRIOR_WEIGHT))
    assert smoothed_reliability(0, 10, 0.5) < smoothed_reliability(0, 1, 0.5) < 0.5


class _Unused:
    """Service never called by the tested tools."""


class _Gds:
    """Returns nothing, or one prediction."""

    def __init__(self, moves):
        self.moves = moves

    def predicted_moves(self, player_id, limit=5):
        """Canned predictions."""
        return self.moves


class _Rumours:
    """Returns nothing, or one rumour."""

    def __init__(self, rumours):
        self.rumours = rumours

    def for_player(self, player_id, status=None):
        """Canned rumours."""
        return self.rumours


@pytest.mark.parametrize("empty", [True, False])
def test_hypothesis_tools_always_label_their_nature(empty):
    moves = [] if empty else [PredictedMove(player_id=9, club_id=5, club_name="Harrowgate FC", probability=0.42,
                                             model_version="fixture")]
    rumours = [] if empty else [RumourOut(rumour_id="r1", player_id=5, club_id=6, club_name="Wexley Athletic",
                                          status="pending", confidence=0.3, source="s", url="u")]
    tools = ToolBox(_Unused(), _Unused(), _Gds(moves), _Rumours(rumours))
    assert tools.run("predicted_moves", {"player_id": 9}).nature == "predicted"
    assert tools.run("rumours", {"player_id": 5}).nature == "rumour"
    if not empty:
        assert tools.run("rumours", {"player_id": 5}).data["rumours"][0]["nature"] == "rumour"
        assert tools.run("predicted_moves", {"player_id": 9}).data["predicted_moves"][0]["nature"] == "predicted"


# ---------- against the test database ----------

SETUP = """
CREATE (p:Player {player_id: -4301, name: 'Testd Reliability Player'}), (c:Club {club_id: -4401, name: 'Testd Club'})
WITH p, c
UNWIND [
  {id: 'test-d-g1', source: 'test-d-good', status: 'confirmed', conf: 0.5, day: 1},
  {id: 'test-d-g2', source: 'test-d-good', status: 'confirmed', conf: 0.5, day: 2},
  {id: 'test-d-g3', source: 'test-d-good', status: 'confirmed', conf: 0.5, day: 3},
  {id: 'test-d-g4', source: 'test-d-good', status: 'contradicted', conf: 0.5, day: 4},
  {id: 'test-d-g5', source: 'test-d-good', status: 'pending', conf: 0.8, day: 20},
  {id: 'test-d-b1', source: 'test-d-bad', status: 'expired', conf: 0.5, day: 5},
  {id: 'test-d-b2', source: 'test-d-bad', status: 'expired', conf: 0.5, day: 6},
  {id: 'test-d-b3', source: 'test-d-bad', status: 'contradicted', conf: 0.5, day: 7},
  {id: 'test-d-b4', source: 'test-d-bad', status: 'pending', conf: 0.8, day: 21},
  {id: 'test-d-n1', source: 'test-d-new', status: 'pending', conf: 0.8, day: 22}
] AS row
CREATE (a:Article {url: 'https://test-d.example/rel/' + row.id, source: row.source, title: row.id,
                   published_at: date('2026-01-01') + duration({days: row.day})})
CREATE (r:Rumour {rumour_id: row.id, status: row.status, confidence: row.conf, quote: row.id})
CREATE (a)-[:REPORTS]->(r), (r)-[:ABOUT]->(p), (r)-[:TARGET]->(c)
"""
CLEANUP = """
MATCH (n) WHERE (n:Rumour AND n.rumour_id STARTS WITH 'test-d-')
   OR (n:Article AND n.url STARTS WITH 'https://test-d.example/rel/')
   OR (n:Player AND n.player_id = -4301) OR (n:Club AND n.club_id = -4401)
DETACH DELETE n
"""


@pytest.mark.neo4j
def test_displayed_confidence_weights_extraction_by_source_reliability(neo4j_db):
    neo4j_db.write(CLEANUP)
    neo4j_db.write(SETUP)
    try:
        service = RumourService(neo4j_db)
        pending = {r.rumour_id: r for r in service.for_player(-4301, "pending")}
        good, bad, new = pending["test-d-g5"], pending["test-d-b4"], pending["test-d-n1"]
        assert list(pending) == ["test-d-n1", "test-d-b4", "test-d-g5"]  # newest first
        assert good.source_reliability > new.source_reliability > bad.source_reliability
        for rumour in (good, bad, new):
            assert rumour.extraction_confidence == 0.8
            assert rumour.confidence == pytest.approx(0.8 * rumour.source_reliability, abs=1e-3)
        overall = neo4j_db.read(
            "MATCH (r:Rumour) WHERE r.status IN ['confirmed', 'contradicted', 'expired'] "
            "RETURN count(CASE WHEN r.status = 'confirmed' THEN 1 END) AS c, count(r) AS d"
        )[0]
        prior = overall["c"] / overall["d"] if overall["d"] else DEFAULT_PRIOR
        assert new.source_reliability == pytest.approx(prior, abs=1e-3)
        assert good.source_reliability == pytest.approx(smoothed_reliability(3, 4, prior), abs=1e-3)
        assert service.source_reliability(["test-d-bad"])["test-d-bad"] == pytest.approx(
            smoothed_reliability(0, 3, prior)
        )
    finally:
        neo4j_db.write(CLEANUP)


class _Broken:
    """Services whose every method fails."""

    def predicted_moves(self, player_id, limit=5):
        """Not implemented yet."""
        raise NotImplementedError

    def for_player(self, player_id, status=None):
        """Database down."""
        raise ConnectionError("down")


def test_failed_hypothesis_tools_keep_their_nature():
    tools = ToolBox(_Unused(), _Unused(), _Broken(), _Broken())
    predicted, rumour = tools.run("predicted_moves", {"player_id": 9}), tools.run("rumours", {"player_id": 5})
    assert predicted.error and predicted.nature == "predicted"
    assert rumour.error and rumour.nature == "rumour"
    assert tools.run("cypher_fallback", {"query": "MATCH (n) DELETE n"}).nature == "verified"
    assert tools.run("unknown_tool", {}).nature == "verified"


MULTI_SOURCE = """
CREATE (p:Player {player_id: -4302, name: 'Testd Multi Source'}), (c:Club {club_id: -4402, name: 'Testd Club Two'})
CREATE (r:Rumour {rumour_id: 'test-d-m1', status: 'pending', confidence: 0.5, quote: 'q'})
CREATE (r)-[:ABOUT]->(p), (r)-[:TARGET]->(c)
WITH r
UNWIND [{source: 'test-d-good', day: 1}, {source: 'test-d-bad', day: 2}] AS row
CREATE (a:Article {url: 'https://test-d.example/rel/m-' + row.source, source: row.source, title: 'm',
                   published_at: date('2026-02-01') + duration({days: row.day})})
CREATE (a)-[:REPORTS]->(r)
"""
MULTI_CLEANUP = """
MATCH (n) WHERE (n:Rumour AND n.rumour_id = 'test-d-m1') OR (n:Article AND n.url STARTS WITH 'https://test-d.example/rel/m-')
   OR (n:Player AND n.player_id = -4302) OR (n:Club AND n.club_id = -4402)
DETACH DELETE n
"""


@pytest.mark.neo4j
def test_a_rumour_is_as_credible_as_its_best_source(neo4j_db):
    neo4j_db.write(CLEANUP)
    neo4j_db.write(MULTI_CLEANUP)
    neo4j_db.write(SETUP)
    neo4j_db.write(MULTI_SOURCE)
    try:
        service = RumourService(neo4j_db)
        [rumour] = service.for_player(-4302)
        reliability = service.source_reliability(["test-d-good", "test-d-bad"])
        assert rumour.source == "test-d-bad"  # the newest article
        assert rumour.sources_count == 2
        assert rumour.source_reliability == pytest.approx(reliability["test-d-good"], abs=1e-3)
    finally:
        neo4j_db.write(MULTI_CLEANUP)
        neo4j_db.write(CLEANUP)
