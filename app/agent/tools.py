"""Tools the agent may call. Owner: D.

Tools never write Cypher of their own: they call the service methods of `app/services/`.
In tests, pass fake services with the same method names.

Every tool returns a `ToolResult`: the data, the nature of what it states (`verified`, `predicted`, `rumour`)
and the graph ids (`Label:id`) the writer may cite as evidence. The only exception to "no Cypher here" is
`cypher_fallback`, which runs LLM-generated Cypher after `CypherValidator` accepted it, in a read transaction.
"""

import json
import re
from datetime import date
from typing import Any, Callable, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.guardrails import CypherValidator
from app.models.agent import Nature
from app.models.players import PlayerSearch
from app.services.clubs import ClubService
from app.services.gds import GdsService
from app.services.players import PlayerService
from app.services.rumours import RumourService

MAX_RESULT_CHARS = 4000

# Nature of what each hypothesis tool states; every other tool states verified facts.
TOOL_NATURES: dict[str, Nature] = {"predicted_moves": "predicted", "rumours": "rumour"}

# Keys of fallback rows that identify a node, and the label they prove.
ID_KEYS = {
    "player_id": "Player",
    "club_id": "Club",
    "competition_id": "Competition",
    "transfer_id": "Transfer",
    "valuation_id": "Valuation",
    "rumour_id": "Rumour",
}


class ToolResult(BaseModel):
    """What a tool hands back to the planner, the writer and the verifier."""

    tool: str
    nature: Nature = "verified"
    evidence_ids: list[str] = Field(default_factory=list)
    data: Any = None
    error: str | None = None

    def to_message(self) -> str:
        """Compact JSON for the LLM, truncated to keep the context small."""
        text = json.dumps(self.model_dump(exclude={"tool"}), default=str, ensure_ascii=False)
        return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "...(truncated)"


class ResolveArgs(BaseModel):
    """Arguments of `resolve_entity`."""

    name: str = Field(description="Player or club name as written by the user")
    kind: Literal["player", "club", "any"] = Field("any", description="What the name refers to")


class SearchArgs(BaseModel):
    """Arguments of `search_players` (same filters as `GET /api/players/search`)."""

    position: str | None = Field(None, description="Goalkeeper, Defender, Midfield or Attack")
    sub_position: str | None = Field(None, description="For example Left-Back, Centre-Back, Centre-Forward")
    max_age: int | None = Field(None, ge=15, le=45)
    max_value_eur: int | None = Field(None, ge=0)
    competition_id: str | None = Field(None, description="Transfermarkt code, for example FR1, GB1, PO1")
    limit: int = Field(10, ge=1, le=50)


class PipelineArgs(BaseModel):
    """Arguments of `club_pipeline`."""

    club_id: int
    since: date | None = Field(None, description="Only transfers on or after this date")
    limit: int = Field(10, ge=1, le=50)


class PathArgs(BaseModel):
    """Arguments of `teammate_path`."""

    from_id: int = Field(description="player_id of the first player")
    to_id: int = Field(description="player_id of the second player")
    max_hops: int = Field(6, ge=1, le=6)


class PlayerArgs(BaseModel):
    """Arguments of the tools about one player."""

    player_id: int


class SimilarArgs(BaseModel):
    """Arguments of `similar_players`."""

    player_id: int
    k: int = Field(5, ge=1, le=20)


class RumourArgs(BaseModel):
    """Arguments of `rumours`."""

    player_id: int
    status: Literal["pending", "confirmed", "expired", "contradicted"] | None = None


class CypherArgs(BaseModel):
    """Arguments of `cypher_fallback`."""

    query: str = Field(description="One read-only Cypher query with a LIMIT of at most 100")


def failure(tool: str, error: str, data: Any = None) -> ToolResult:
    """A failed call, labelled with the nature of its tool so that a failed hypothesis tool never reads `verified`."""
    return ToolResult(tool=tool, nature=TOOL_NATURES.get(tool, "verified"), data=data, error=error)


def age_on(birth: date | None, today: date) -> int | None:
    """Age in whole years at `today`, None without a birth date."""
    if birth is None:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def evidence_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    """Collect `Label:id` evidence from the id columns of free-form rows."""
    found: list[str] = []
    for row in rows:
        for key, value in row.items():
            if key in ID_KEYS and value is not None:
                found.append(f"{ID_KEYS[key]}:{value}")
    return list(dict.fromkeys(found))


class ToolBox:
    """Bundles the services the agent's tools are built on."""

    def __init__(self, players: PlayerService, clubs: ClubService, gds: GdsService, rumours: RumourService):
        self.players = players
        self.clubs = clubs
        self.gds = gds
        self.rumours = rumours
        self.validator = CypherValidator()
        self.today = date.today()

    # ---------- one method per tool (each calls one service method) ----------

    def resolve_entity(self, name: str, kind: str = "any") -> ToolResult:
        """Players and/or clubs matching a name, through the full-text indexes."""
        matches = []
        if kind in ("player", "any"):
            matches += self.players.resolve(name, limit=5)
        if kind in ("club", "any"):
            matches += self.clubs.resolve(name, limit=5)
        matches.sort(key=lambda match: match.score, reverse=True)
        evidence = [f"{match.kind.capitalize()}:{match.id}" for match in matches]
        return ToolResult(tool="resolve_entity", evidence_ids=evidence, data=[m.model_dump() for m in matches])

    def search_players(self, **criteria: Any) -> ToolResult:
        """Players matching position, age, value and competition filters."""
        page = self.players.search(PlayerSearch(**criteria))
        rows = []
        for player in page.items:
            row = player.model_dump(mode="json")
            row["age"] = age_on(player.date_of_birth, self.today)
            rows.append(row)
        evidence = [f"Player:{player.player_id}" for player in page.items]
        evidence += [f"Club:{player.club_id}" for player in page.items if player.club_id is not None]
        data = {"count": len(rows), "reference_date": self.today.isoformat(), "players": rows}
        return ToolResult(tool="search_players", evidence_ids=list(dict.fromkeys(evidence)), data=data)

    def club_pipeline(self, club_id: int, since: date | None = None, limit: int = 10) -> ToolResult:
        """Outgoing transfers of a club grouped by destination competition."""
        rows = self.clubs.pipeline(club_id, since=since, limit=limit)
        evidence = [f"Club:{club_id}"] + [f"Competition:{row.competition_id}" for row in rows]
        data = {"club_id": club_id, "rows": [row.model_dump() for row in rows]}
        return ToolResult(tool="club_pipeline", evidence_ids=evidence, data=data)

    def teammate_path(self, from_id: int, to_id: int, max_hops: int = 6) -> ToolResult:
        """Shortest chain of teammates between two players (at most 6 hops)."""
        path = self.players.teammate_path(from_id, to_id, max_hops=max_hops)
        if path is None:
            data = {"found": False, "from_id": from_id, "to_id": to_id, "max_hops": max_hops}
            return ToolResult(tool="teammate_path", evidence_ids=[f"Player:{from_id}", f"Player:{to_id}"], data=data)
        evidence = [f"Player:{step.player_id}" for step in path.players]
        return ToolResult(tool="teammate_path", evidence_ids=evidence, data={"found": True, **path.model_dump()})

    def value_trend(self, player_id: int) -> ToolResult:
        """Market-value history of a player, oldest first."""
        points = self.players.value_trend(player_id)
        evidence = [f"Player:{player_id}"] + [f"Valuation:{player_id}-{point.as_of.isoformat()}" for point in points]
        data = {"player_id": player_id, "valuations": [point.model_dump(mode="json") for point in points]}
        return ToolResult(tool="value_trend", evidence_ids=evidence, data=data)

    def similar_players(self, player_id: int, k: int = 5) -> ToolResult:
        """Nearest players by FastRP embedding (computed from facts, not a prediction of a move)."""
        neighbours = self.gds.similar_players(player_id, k=k)
        evidence = [f"Player:{player_id}"] + [f"Player:{player.player_id}" for player in neighbours]
        data = {"player_id": player_id, "similar": [player.model_dump() for player in neighbours]}
        return ToolResult(tool="similar_players", evidence_ids=evidence, data=data)

    def predicted_moves(self, player_id: int) -> ToolResult:
        """Destinations predicted by the link-prediction model: hypotheses, never facts."""
        moves = self.gds.predicted_moves(player_id, limit=5)
        evidence = [f"PREDICTED_MOVE:{move.player_id}->{move.club_id}" for move in moves]
        data = {"player_id": player_id, "predicted_moves": [move.model_dump() for move in moves]}
        return ToolResult(tool="predicted_moves", nature="predicted", evidence_ids=evidence, data=data)

    def player_rumours(self, player_id: int, status: str | None = None) -> ToolResult:
        """Press rumours about a player, with source, status and confidence: never facts."""
        found = self.rumours.for_player(player_id, status)
        evidence = [f"Rumour:{rumour.rumour_id}" for rumour in found]
        data = {"player_id": player_id, "rumours": [rumour.model_dump(mode="json") for rumour in found]}
        return ToolResult(tool="rumours", nature="rumour", evidence_ids=evidence, data=data)

    def cypher_fallback(self, query: str) -> ToolResult:
        """Last resort: validated read-only Cypher, run in a read transaction."""
        check = self.validator.validate(query)
        if not check.valid:
            return failure("cypher_fallback", "; ".join(check.reasons), data={"query": query})
        rows = self.players.client.read(check.query)
        nature: Nature = "verified"
        if re.search(r"\bRumour\b|\bArticle\b", check.query):
            nature = "rumour"
        elif "PREDICTED_" in check.query:
            nature = "predicted"
        data = {"query": check.query, "rows": rows[:100]}
        return ToolResult(tool="cypher_fallback", nature=nature, evidence_ids=evidence_from_rows(rows), data=data)

    # ---------- LangChain wiring ----------

    def specs(self) -> dict[str, tuple[Callable[..., ToolResult], type[BaseModel], str]]:
        """Name -> (method, argument schema, description) of every tool."""
        return {
            "resolve_entity": (self.resolve_entity, ResolveArgs, "Find player or club ids from a name."),
            "search_players": (
                self.search_players,
                SearchArgs,
                "Search players by position, sub-position, maximum age, maximum market value, competition.",
            ),
            "club_pipeline": (
                self.club_pipeline,
                PipelineArgs,
                "Outgoing transfers of a club grouped by destination competition (counts and fees).",
            ),
            "teammate_path": (self.teammate_path, PathArgs, "Shortest chain of teammates between two players."),
            "value_trend": (self.value_trend, PlayerArgs, "Market-value history of a player."),
            "similar_players": (self.similar_players, SimilarArgs, "Players with a similar career profile."),
            "predicted_moves": (
                self.predicted_moves,
                PlayerArgs,
                "Clubs a player is PREDICTED to join by the ML model (a hypothesis, not a fact).",
            ),
            "rumours": (
                self.player_rumours,
                RumourArgs,
                "Press rumours about a player (unconfirmed unless their status says so).",
            ),
            "cypher_fallback": (
                self.cypher_fallback,
                CypherArgs,
                "Only when no other tool fits: one read-only Cypher query on the documented schema, LIMIT <= 100.",
            ),
        }

    def run(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Validate the arguments and run one tool; errors come back inside the result, never raised."""
        spec = self.specs().get(name)
        if spec is None:
            return failure(name, f"unknown tool {name!r}")
        method, schema, _ = spec
        try:
            parsed = schema.model_validate(arguments or {})
            return method(**parsed.model_dump())
        except NotImplementedError:
            return failure(name, "this service is not implemented yet")
        except Exception as error:  # pylint: disable=broad-exception-caught
            return failure(name, f"{type(error).__name__}: {error}"[:300])

    def as_langchain_tools(self) -> list[StructuredTool]:
        """Tool definitions to bind to the planner model (execution goes through `run`)."""
        return [
            StructuredTool.from_function(
                func=lambda _name=name, **kwargs: self.run(_name, kwargs).to_message(),
                name=name,
                description=description,
                args_schema=schema,
            )
            for name, (_, schema, description) in self.specs().items()
        ]
