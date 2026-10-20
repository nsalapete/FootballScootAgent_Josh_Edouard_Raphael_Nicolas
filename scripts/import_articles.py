"""Import press articles and extract transfer rumours. Owner: D.

Run with `python -m scripts.import_articles` (default feeds) or
`python -m scripts.import_articles --feed https://www.theguardian.com/football/transfers/rss --max 20`.
It spends LLM credit (one small-model call per new article): articles already in the graph are skipped.
"""

import argparse

from app.config import get_settings
from app.database.neo4j import Neo4jClient
from app.models.rumours import ImportRequest
from app.services.rumours import RumourService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="Import press articles and extract transfer rumours.")
    parser.add_argument("--feed", action="append", default=[], help="RSS/Atom or GDELT DOC URL (repeatable)")
    parser.add_argument("--max", type=int, default=50, help="Maximum number of articles to read (1-500)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the article import."""
    args = parse_args(argv)
    settings = get_settings()
    client = Neo4jClient(
        settings.neo4j_uri,
        (settings.neo4j_user, settings.neo4j_password),
        settings.neo4j_database,
        settings.neo4j_query_timeout_s,
    )
    try:
        if not client.ping():
            raise SystemExit(f"Neo4j unreachable at {settings.neo4j_uri}.")
        report = RumourService(client).import_articles(ImportRequest(feed_urls=args.feed, max_articles=args.max))
        print(", ".join(f"{name}: {value}" for name, value in report.model_dump().items()))
    finally:
        client.close()


if __name__ == "__main__":
    main()
