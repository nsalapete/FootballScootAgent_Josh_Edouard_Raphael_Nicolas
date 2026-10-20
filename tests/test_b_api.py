"""HTTP layer of B's routes: status codes, error bodies and Swagger examples. Owner: B."""

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from app.database.neo4j import get_db
from app.main import create_app

UNKNOWN = 999999


def assert_error(response, code, fragment):
    """The response has status `code` and an `ErrorResponse` body whose detail contains `fragment`."""
    assert response.status_code == code, response.text
    body = response.json()
    assert isinstance(body["detail"], str)
    assert fragment in body["detail"]
    return body


def assert_invalid(response, field):
    """422 whose `errors` names `field`."""
    body = assert_error(response, 422, "Requête invalide")
    assert field in {error["field"] for error in body["errors"]}
    return body


# ---------- Without a database: validation happens before any query ----------


@pytest.mark.parametrize(
    ("url", "field"),
    [
        ("/api/players/search?max_age=10", "max_age"),
        ("/api/players/search?max_age=abc", "max_age"),
        ("/api/players/search?max_value_eur=-1", "max_value_eur"),
        ("/api/players/search?limit=0", "limit"),
        ("/api/players/search?limit=101", "limit"),
        ("/api/players/path?from=1&to=4&max_hops=7", "max_hops"),
        ("/api/players/path?from=1&to=4&max_hops=0", "max_hops"),
        ("/api/players/path?to=4", "from"),
        ("/api/players/path?from=abc&to=4", "from"),
        ("/api/players/resolve?name=", "name"),
        ("/api/players/resolve?name=" + "x" * 101, "name"),
        ("/api/players/resolve", "name"),
        ("/api/players/abc", "player_id"),
        ("/api/clubs/7/pipeline?since=not-a-date", "since"),
        ("/api/clubs/7/pipeline?limit=51", "limit"),
        ("/api/clubs/abc/pipeline", "club_id"),
        ("/api/clubs/resolve?name=Alvorada&limit=21", "limit"),
    ],
)
def test_invalid_parameters_give_clean_422(api_no_db, url, field):
    body = assert_invalid(api_no_db.get(url), field)
    error = next(error for error in body["errors"] if error["field"] == field)
    assert error["location"] in {"query", "path"}
    assert error["message"]


def test_invalid_body_gives_clean_422_on_other_routers(api_no_db):
    body = assert_invalid(api_no_db.post("/api/agent/ask", json={}), "question")
    assert body["errors"][0]["location"] == "body"


def test_neo4j_down_gives_503():
    class DownDatabase:
        """Every query fails as if Neo4j were stopped."""

        def read(self, query, params=None):
            raise ServiceUnavailable("down")

    application = create_app()
    application.dependency_overrides[get_db] = DownDatabase
    with TestClient(application) as client:
        assert_error(client.get("/api/players/1"), 503, "Neo4j")
        assert_error(client.get("/api/clubs/7/pipeline"), 503, "Neo4j")


# ---------- Swagger ----------


def test_swagger_documents_errors_and_examples(api_no_db):
    spec = api_no_db.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in ("PlayerSummary", "TeammatePath", "PipelineRow", "EntityMatch", "ErrorResponse", "Valuation"):
        assert schemas[name].get("examples"), name
    error_ref = "#/components/schemas/ErrorResponse"
    for path in ("/api/players/search", "/api/players/path", "/api/clubs/{club_id}/pipeline", "/api/agent/ask"):
        operation = next(iter(spec["paths"][path].values()))
        assert operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"] == error_ref, path
    assert "HTTPValidationError" not in schemas
    path_404 = spec["paths"]["/api/players/path"]["get"]["responses"]["404"]
    assert path_404["content"]["application/json"]["schema"]["$ref"] == error_ref


# ---------- With the mini graph ----------


@pytest.mark.neo4j
def test_search_ok_and_next_page(api):
    first = api.get("/api/players/search", params={"sub_position": "Left-Back", "limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert [player["player_id"] for player in body["items"]] == [1, 5]
    second = api.get(
        "/api/players/search", params={"sub_position": "Left-Back", "limit": 2, "cursor": body["next_cursor"]}
    )
    assert [player["player_id"] for player in second.json()["items"]] == [14]
    assert second.json()["next_cursor"] is None


@pytest.mark.neo4j
@pytest.mark.parametrize("cursor", ["garbage", "e30", "eyJ2IjoiYSIsImlkIjoxfQ"])
def test_search_invalid_cursor_gives_422(api, cursor):
    body = assert_error(api.get("/api/players/search", params={"cursor": cursor}), 422, "Curseur")
    assert body.get("errors") is None


@pytest.mark.neo4j
def test_search_without_match_is_an_empty_page(api):
    response = api.get("/api/players/search", params={"competition_id": "ES1"})
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.neo4j
def test_path_ok(api):
    response = api.get("/api/players/path", params={"from": 1, "to": 4})
    assert response.status_code == 200
    assert response.json()["hops"] == 4


@pytest.mark.neo4j
@pytest.mark.parametrize(
    ("from_id", "to_id", "fragment"),
    [
        (UNKNOWN, 4, f"Joueur inconnu : {UNKNOWN}"),
        (1, UNKNOWN, f"Joueur inconnu : {UNKNOWN}"),
        (14, 7, "Aucun chemin de coéquipiers en 6 sauts"),
        (1, 10, "Aucun chemin"),
    ],
)
def test_path_404(api, from_id, to_id, fragment):
    assert_error(api.get("/api/players/path", params={"from": from_id, "to": to_id}), 404, fragment)


@pytest.mark.neo4j
def test_path_404_with_smaller_bound(api):
    assert_error(api.get("/api/players/path", params={"from": 1, "to": 4, "max_hops": 3}), 404, "en 3 sauts")


@pytest.mark.neo4j
def test_get_player(api):
    response = api.get("/api/players/3")
    assert response.status_code == 200
    assert response.json()["club_name"] == "Kingsbridge United"
    assert response.json()["date_of_birth"] == "2000-02-17"
    assert_error(api.get(f"/api/players/{UNKNOWN}"), 404, f"Joueur inconnu : {UNKNOWN}")


@pytest.mark.neo4j
def test_player_valuations(api):
    response = api.get("/api/players/1/valuations")
    assert response.status_code == 200
    assert [point["as_of"] for point in response.json()] == ["2022-06-30", "2023-06-30", "2024-12-15"]
    assert api.get("/api/players/16/valuations").json() == []
    assert_error(api.get(f"/api/players/{UNKNOWN}/valuations"), 404, "Joueur inconnu")


@pytest.mark.neo4j
def test_resolve_routes(api):
    players = api.get("/api/players/resolve", params={"name": "Gabriel"}).json()
    assert {match["id"] for match in players} == {8, 9}
    clubs = api.get("/api/clubs/resolve", params={"name": "Alvorada"}).json()
    assert clubs[0] == {"kind": "club", "id": 7, "name": "SC Alvorada", "score": clubs[0]["score"]}


@pytest.mark.neo4j
def test_pipeline_ok_and_empty(api):
    response = api.get("/api/clubs/7/pipeline", params={"since": "2022-08-01"})
    assert response.status_code == 200
    assert [row["competition_id"] for row in response.json()] == ["FR1", "PO1"]
    assert api.get("/api/clubs/4/pipeline").json() == []  # known club, never sold


@pytest.mark.neo4j
def test_pipeline_unknown_club_gives_404(api):
    assert_error(api.get(f"/api/clubs/{UNKNOWN}/pipeline"), 404, f"Club inconnu : {UNKNOWN}")


@pytest.mark.neo4j
def test_other_routers_still_reach_their_paths(api):
    # Fixed paths of the network router must not be captured by /api/players/{player_id} or /api/clubs/resolve.
    assert api.get("/api/clubs/hubs").status_code != 422
    assert api.get("/api/players/1/similar").status_code != 422
