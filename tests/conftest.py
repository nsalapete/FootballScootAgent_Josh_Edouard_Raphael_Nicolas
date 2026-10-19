"""Shared pytest fixtures. Owner: A (add your own fixtures in your own test files or a conftest of your own).

- `neo4j_db`: client bound to the test Neo4j (`make test-db`), wiped and loaded with the mini graph once
  per session. Tests using it are skipped when the test database is unreachable.
- `api`: FastAPI TestClient whose database dependency points to `neo4j_db`.
- `api_no_db`: TestClient with a fake database that is always down (no Neo4j needed).
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database.neo4j import Neo4jClient, get_db
from app.main import create_app
from scripts.cypher_files import run_file

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "scripts" / "constraints.cypher"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Tests never call a paid LLM, whatever `.env` says."""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def neo4j_db():
    """Test database loaded with constraints, mini_graph.cypher, then any other fixtures/*.cypher."""
    uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7688")
    password = os.getenv("NEO4J_TEST_PASSWORD", "testpassword")
    client = Neo4jClient(uri, ("neo4j", password))
    if not client.ping():
        client.close()
        pytest.skip("Neo4j de test injoignable : lancez `make test-db`")
    client.write("MATCH (n) DETACH DELETE n")
    run_file(client, CONSTRAINTS)
    run_file(client, FIXTURES / "mini_graph.cypher")
    for extra in sorted(FIXTURES.glob("*.cypher")):
        if extra.name != "mini_graph.cypher":
            run_file(client, extra)
    yield client
    client.close()


@pytest.fixture()
def api(neo4j_db):
    """API client wired to the test database."""
    application = create_app()
    application.dependency_overrides[get_db] = lambda: neo4j_db
    with TestClient(application) as client:
        yield client


class _DownDatabase:
    """Stands in for Neo4j when it is unreachable."""

    def ping(self) -> bool:
        return False


@pytest.fixture()
def api_no_db():
    """API client whose database is always down."""
    application = create_app()
    application.dependency_overrides[get_db] = _DownDatabase
    with TestClient(application) as client:
        yield client
