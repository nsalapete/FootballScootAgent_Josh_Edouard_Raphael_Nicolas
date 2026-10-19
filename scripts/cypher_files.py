"""Helpers to run `.cypher` files statement by statement. Owner: A.

Used by the ingestion (constraints) and by the test fixtures (mini graph).
"""

from pathlib import Path

from app.database.neo4j import Neo4jClient


def split_statements(text: str) -> list[str]:
    """Split a Cypher file on `;`, dropping `//` comment lines and empty statements."""
    lines = [line for line in text.splitlines() if not line.strip().startswith("//")]
    return [statement.strip() for statement in "\n".join(lines).split(";") if statement.strip()]


def run_file(db: Neo4jClient, path: Path) -> int:
    """Run every statement of `path` in its own write transaction; return how many ran."""
    statements = split_statements(path.read_text(encoding="utf-8"))
    for statement in statements:
        db.write(statement)
    return len(statements)
