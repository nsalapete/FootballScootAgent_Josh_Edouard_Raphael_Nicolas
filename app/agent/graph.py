"""LangGraph state graph of the scouting agent. Owner: D."""

from app.agent.tools import ToolBox
from app.models.agent import AgentAnswer


class ScoutAgent:
    """Router, planner, tools, writer and evidence verifier wired as a LangGraph graph."""

    def __init__(self, tools: ToolBox):
        self.tools = tools

    def ask(self, question: str) -> AgentAnswer:
        """Answer `question`; every claim carries its nature and evidence, and the trace lists each tool call."""
        raise NotImplementedError
