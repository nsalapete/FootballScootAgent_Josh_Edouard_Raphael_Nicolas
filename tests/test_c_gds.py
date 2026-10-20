"""GdsService: unit tests with a fake client, then GDS runs on the mini graph. Owner: C."""

import pytest
from neo4j.exceptions import Neo4jError

from app.services.gds import DROP, FASTRP, PAGERANK, GdsService

CLEANUP = (
    "MATCH (c:Club) REMOVE c.pagerank, c.community_id",
    "MATCH (p:Player) REMOVE p.embedding",
)


class RecordingClient:
    """Records every query; fails on the query given as `fail_on`."""

    def __init__(self, fail_on: str | None = None, rows=None):
        self.calls = []
        self.fail_on = fail_on
        self.rows = rows or []

    def write(self, query, params=None):
        """Record a write; raise if it is the failing query."""
        self.calls.append((query, params))
        if query == self.fail_on:
            raise RuntimeError("algorithm failed")
        return []

    def read(self, query, params=None):
        """Record a read and return the canned rows."""
        self.calls.append((query, params))
        return self.rows


# ---------- without a database ----------


def test_refresh_drops_both_projections_and_reports():
    client = RecordingClient()
    report = GdsService(client).refresh()
    assert report.projections == ["clubs", "teammates"]
    assert report.algorithms == ["pageRank", "louvain", "fastRP"]
    assert report.duration_s >= 0
    dropped = [params["graph"] for query, params in client.calls if query == DROP]
    assert [name.split("-")[0] for name in dropped] == ["clubs", "teammates"]


def test_refresh_drops_projections_even_when_an_algorithm_fails():
    client = RecordingClient(fail_on=FASTRP)
    with pytest.raises(RuntimeError):
        GdsService(client).refresh()
    assert sum(1 for query, _ in client.calls if query == DROP) == 2


def test_refresh_passes_graph_names_as_parameters():
    client = RecordingClient()
    GdsService(client).refresh()
    for query, params in client.calls:
        assert params["graph"] not in query  # never formatted into the Cypher text
    assert any(query == PAGERANK for query, _ in client.calls)


def test_reads_map_rows_to_models():
    service = GdsService(RecordingClient(rows=[{"club_id": 7, "name": "SC Alvorada", "score": 1.5}]))
    assert service.hubs(3)[0].club_id == 7
    assert service.client.calls[0][1] == {"limit": 3}


# ---------- on the mini graph (needs the GDS plugin) ----------


@pytest.fixture(name="refreshed", scope="module")
def refreshed_fixture(neo4j_db):
    """Run `refresh` once on the mini graph, then remove every property it wrote."""
    try:
        neo4j_db.read("RETURN gds.version() AS version")
    except Neo4jError:
        pytest.skip("GDS plugin missing from the test database")
    service = GdsService(neo4j_db)
    report = service.refresh()
    yield service, report
    for query in CLEANUP:
        neo4j_db.write(query)


@pytest.mark.neo4j
@pytest.mark.gds
def test_every_club_has_a_pagerank(refreshed, neo4j_db):
    rows = neo4j_db.read("MATCH (c:Club) RETURN count(c) AS clubs, count(c.pagerank) AS scored")
    assert rows[0]["clubs"] == 8 and rows[0]["scored"] == 8
    assert refreshed[1].projections == ["clubs", "teammates"]


@pytest.mark.neo4j
@pytest.mark.gds
def test_projections_are_dropped(refreshed, neo4j_db):
    assert refreshed
    names = [row["graphName"] for row in neo4j_db.read("CALL gds.graph.list() YIELD graphName RETURN graphName")]
    assert not [name for name in names if name.startswith(("clubs-", "teammates-"))]


@pytest.mark.neo4j
@pytest.mark.gds
def test_hubs_sorted_by_pagerank(refreshed):
    hubs = refreshed[0].hubs(8)
    assert len(hubs) == 8
    assert [hub.score for hub in hubs] == sorted((hub.score for hub in hubs), reverse=True)
    assert len(refreshed[0].hubs(2)) == 2


@pytest.mark.neo4j
@pytest.mark.gds
def test_alvorada_community_exists(refreshed):
    communities = refreshed[0].communities(min_size=2)
    assert any("SC Alvorada" in community.clubs for community in communities)
    assert all(community.size >= 2 and community.size == len(community.clubs) for community in communities)
    assert [community.size for community in communities] == sorted((c.size for c in communities), reverse=True)
    assert sum(community.size for community in refreshed[0].communities(min_size=1)) == 8


@pytest.mark.neo4j
@pytest.mark.gds
def test_similar_players_returns_k_others(refreshed):
    neighbours = refreshed[0].similar_players(1, k=5)
    assert len(neighbours) == 5
    assert 1 not in [player.player_id for player in neighbours]
    assert [player.similarity for player in neighbours] == sorted((p.similarity for p in neighbours), reverse=True)


@pytest.mark.neo4j
@pytest.mark.gds
def test_similar_players_unknown_or_isolated(refreshed):
    assert not refreshed[0].similar_players(-424242, k=5)
    # Samuel Okafor (16) has no teammate: zero embedding, cosine undefined, so no neighbour.
    assert not refreshed[0].similar_players(16, k=5)


@pytest.mark.neo4j
def test_predicted_moves_on_the_mini_graph(neo4j_db):
    moves = GdsService(neo4j_db).predicted_moves(9)
    assert [(move.club_id, move.club_name, move.probability) for move in moves] == [(5, "Harrowgate FC", 0.42)]
    assert moves[0].nature == "predicted" and moves[0].model_version == "fixture"
    assert not GdsService(neo4j_db).predicted_moves(1)
    assert not GdsService(neo4j_db).predicted_moves(9, limit=1)[1:]
