"""Load transfermarkt-datasets (DuckDB) into Neo4j. Owner: A.

Run with `python -m scripts.seed_data` (or `make seed`, or the `ingest` Docker service).
Must be idempotent: running it twice gives the same graph (MERGE on unique keys).
"""


def main() -> None:
    """Entry point of the ingestion."""
    raise SystemExit("Ingestion pas encore implémentée (rôle A).")


if __name__ == "__main__":
    main()
