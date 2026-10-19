"""Thin wrapper around the Neo4j Python driver, shared by every service.

Owner: A. Services receive a `Neo4jClient` (dependency injection) and never open a driver themselves.
All Cypher must be parameterised: pass values in `params`, never format them into the query string.
"""

from functools import lru_cache
from typing import Any

from neo4j import GraphDatabase, ManagedTransaction, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from app.config import get_settings

Record = dict[str, Any]


class Neo4jClient:
    """Runs parameterised Cypher in read or write transactions with a time limit."""

    def __init__(self, uri: str, auth: tuple[str, str], database: str = "neo4j", timeout_s: float = 10.0):
        self._driver = GraphDatabase.driver(uri, auth=auth)
        self._database = database
        self._timeout_s = timeout_s

    def close(self) -> None:
        """Close the underlying driver and its connection pool."""
        self._driver.close()

    def ping(self) -> bool:
        """Return True when the database answers, False otherwise (never raises)."""
        try:
            self._driver.verify_connectivity()
            return True
        except (ServiceUnavailable, Neo4jError, OSError, ValueError):
            return False

    def read(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Run `query` in a read transaction: the server rejects any write it contains."""
        work = self._work(query, params or {})
        with self._driver.session(database=self._database) as session:
            return session.execute_read(work)

    def write(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Run `query` in a write transaction (ingestion, GDS write-back, rumour import)."""
        work = self._work(query, params or {})
        with self._driver.session(database=self._database) as session:
            return session.execute_write(work)

    def _work(self, query: str, params: dict[str, Any]):
        """Build a transaction function bounded by the configured timeout."""

        @unit_of_work(timeout=self._timeout_s)
        def run(tx: ManagedTransaction) -> list[Record]:
            return [record.data() for record in tx.run(query, params)]

        return run


@lru_cache
def get_client() -> Neo4jClient:
    """Return the process-wide client built from settings (cached)."""
    settings = get_settings()
    return Neo4jClient(
        settings.neo4j_uri,
        (settings.neo4j_user, settings.neo4j_password),
        settings.neo4j_database,
        settings.neo4j_query_timeout_s,
    )


def get_db() -> Neo4jClient:
    """FastAPI dependency. Tests override it with a client bound to the test database."""
    return get_client()
