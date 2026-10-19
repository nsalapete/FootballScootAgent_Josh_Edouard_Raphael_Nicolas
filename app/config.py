"""Application settings, read from environment variables and `.env`.

Owner: B. Every variable is listed in `.env.example`; add new ones there too.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Each field maps to the upper-case variable of the same name."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me-please"
    neo4j_database: str = "neo4j"
    neo4j_query_timeout_s: float = 10.0

    api_key: str = ""

    llm_provider: str = "fake"
    llm_model_small: str = "claude-haiku-5-5"
    llm_model_large: str = "claude-sonnet-5-5"
    llm_base_url: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    duckdb_path: str = "data/transfermarkt-datasets.duckdb"
    scope_competitions: str = "GB1,ES1,IT1,L1,FR1,PO1,NL1"
    scope_from_season: int = 2015

    @property
    def competitions(self) -> list[str]:
        """Competition codes kept by the ingestion, parsed from `SCOPE_COMPETITIONS`."""
        return [code.strip() for code in self.scope_competitions.split(",") if code.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (cached)."""
    return Settings()
