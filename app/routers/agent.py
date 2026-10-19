"""Agent endpoint. Owner: D."""

from fastapi import APIRouter, Depends

from app.agent.graph import ScoutAgent
from app.agent.tools import ToolBox
from app.database.neo4j import Neo4jClient, get_db
from app.models.agent import AgentAnswer, AgentQuestion
from app.services.clubs import ClubService
from app.services.gds import GdsService
from app.services.players import PlayerService
from app.services.rumours import RumourService

router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_agent(db: Neo4jClient = Depends(get_db)) -> ScoutAgent:
    """Build the agent and its tools for one request."""
    tools = ToolBox(PlayerService(db), ClubService(db), GdsService(db), RumourService(db))
    return ScoutAgent(tools)


@router.post("/ask", response_model=AgentAnswer)
def ask(body: AgentQuestion, agent: ScoutAgent = Depends(get_agent)) -> AgentAnswer:
    """Ask the scouting agent a question in natural language; the answer exposes its tool trace."""
    return agent.ask(body.question)
