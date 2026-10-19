"""Smoke tests of the skeleton: the app starts and exposes the agreed routes. Owner: B."""

from app.config import get_settings

EXPECTED_ROUTES = {
    ("get", "/api/health"),
    ("get", "/api/players/search"),
    ("get", "/api/players/path"),
    ("get", "/api/clubs/{club_id}/pipeline"),
    ("get", "/api/clubs/hubs"),
    ("get", "/api/network/communities"),
    ("get", "/api/players/{player_id}/similar"),
    ("get", "/api/players/{player_id}/predicted-moves"),
    ("post", "/api/admin/gds/refresh"),
    ("get", "/api/players/{player_id}/rumours"),
    ("post", "/api/rumours/import"),
    ("post", "/api/agent/ask"),
}


def test_swagger_is_served(api_no_db):
    response = api_no_db.get("/docs")
    assert response.status_code == 200


def test_contract_routes_exist(api_no_db):
    paths = api_no_db.get("/openapi.json").json()["paths"]
    found = {(method, path) for path, methods in paths.items() for method in methods}
    assert EXPECTED_ROUTES <= found


def test_health_reports_degraded_without_neo4j(api_no_db):
    response = api_no_db.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "neo4j": False}


def test_protected_endpoints_reject_missing_key(api_no_db, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    get_settings.cache_clear()
    try:
        assert api_no_db.post("/api/admin/gds/refresh").status_code == 401
        assert api_no_db.post("/api/rumours/import", json={}).status_code == 401
    finally:
        get_settings.cache_clear()
