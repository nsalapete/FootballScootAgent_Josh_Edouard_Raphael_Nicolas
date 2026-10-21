"""The Docker stack seen from outside, through the proxy (`make docker-up`). Owner: B.

Skipped when nothing answers on http://localhost/api/health (stack not running, e.g. in CI).
Override the address with `STACK_URL`.
"""

import os

import httpx
import pytest

STACK_URL = os.getenv("STACK_URL", "http://localhost")


def _stack_is_up() -> bool:
    """True when the proxy answers the health route."""
    try:
        return httpx.get(f"{STACK_URL}/api/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _stack_is_up(), reason="stack Docker arrêtée : lancez `make docker-up`")


@pytest.fixture(name="client", scope="module")
def client_fixture():
    """HTTP client aimed at the proxy."""
    with httpx.Client(base_url=STACK_URL, timeout=10) as client:
        yield client


def test_api_and_neo4j_are_healthy_through_the_proxy(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "neo4j": True}


def test_proxy_hides_its_version_and_adds_security_headers(client):
    response = client.get("/api/health")
    assert response.headers.get("server") == "nginx"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"


def test_swagger_is_served_through_the_proxy(client):
    assert client.get("/docs").status_code == 200
    assert "/api/players/search" in client.get("/openapi.json").json()["paths"]


def test_protected_route_needs_the_api_key(client):
    response = client.post("/api/admin/gds/refresh")
    assert response.status_code in (401, 503)  # 503 when API_KEY is not set in .env


def test_agent_route_is_rate_limited_by_the_proxy(client):
    # GET on a POST route: 405 from the API, so no LLM call; the 7th request in a burst exceeds 1 + 5 allowed.
    # A run less than a minute earlier may have emptied the budget already: then every answer is 429.
    codes = [client.get("/api/agent/ask").status_code for _ in range(8)]
    assert set(codes) <= {405, 429}
    assert codes[-1] == 429
