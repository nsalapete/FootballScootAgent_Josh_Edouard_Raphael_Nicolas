"""Agent endpoint. Owner: D."""

from fastapi import APIRouter, Depends, Security
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.graph import ScoutAgent
from app.agent.tools import ToolBox
from app.config import Settings, get_settings
from app.database.neo4j import Neo4jClient, get_db
from app.models.agent import AgentAnswer, AgentQuestion
from app.security import api_key_header, require_api_key
from app.services.clubs import ClubService
from app.services.gds import GdsService
from app.services.players import PlayerService
from app.services.rumours import RumourService

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentSettings(BaseSettings):
    """Role D switch, read like every setting: from the environment, then `.env` (so `make run` sees it too)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    agent_require_api_key: bool = False


def agent_api_key(
    provided: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Require `X-API-Key` when `AGENT_REQUIRE_API_KEY=true` (production): every question spends LLM credit.

    Off by default, so local runs, tests and the mini-graph demo stay open; nginx limits the route to 10 requests
    per minute per IP in any case. Switched on without `API_KEY`, the route fails closed with 503.
    """
    if AgentSettings().agent_require_api_key:
        require_api_key(provided, settings)


def get_agent(db: Neo4jClient = Depends(get_db)) -> ScoutAgent:
    """Build the agent and its tools for one request."""
    tools = ToolBox(PlayerService(db), ClubService(db), GdsService(db), RumourService(db))
    return ScoutAgent(tools)


@router.post("/ask", response_model=AgentAnswer, dependencies=[Depends(agent_api_key)])
def ask(body: AgentQuestion, agent: ScoutAgent = Depends(get_agent)) -> AgentAnswer:
    """Ask the scouting agent a question in natural language; the answer exposes its tool trace.

    Send `X-API-Key` when the server runs with `AGENT_REQUIRE_API_KEY=true` (it spends LLM credit).
    """
    return agent.ask(body.question)
