"""Tests of the LangGraph agent with the scripted fake model and fake services (no network). Owner: D."""

from datetime import date

import pytest
from langchain_core.messages import AIMessage

from app.agent.evidence import GraphEvidence, numbers_in_text, parse_evidence
from app.agent.graph import AgentDraft, RouteDecision, ScoutAgent, expected_nature, out_of_scope_reason, pick_match
from app.agent.tools import ToolBox, age_on, evidence_from_rows
from app.llm.client import FakeChatModel, tool_call
from app.models.agent import Claim
from app.models.clubs import PipelineRow
from app.models.common import EntityMatch, Page
from app.models.network import PredictedMove
from app.models.players import PlayerSummary, Valuation
from app.models.rumours import RumourOut
from app.services.clubs import ClubService
from app.services.gds import GdsService
from app.services.players import PlayerService
from app.services.rumours import RumourService

# ---------- fakes mirroring tests/fixtures/mini_graph.cypher ----------


class FakeClient:
    """Records the queries the fallback would run."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []

    def read(self, query, params=None):
        """Return the canned rows."""
        self.queries.append((query, params))
        return self.rows


class FakePlayers:
    """Stands in for PlayerService."""

    def __init__(self):
        self.client = FakeClient([{"player_id": 3, "name": "Joao Ferreira"}])

    def resolve(self, name, limit=5):
        """Full-text lookalike on a few names."""
        people = {1: "Lucas Marchand", 3: "Joao Ferreira", 8: "Gabriel Souza", 9: "Gabriel Rocha"}
        words = name.lower().split()
        found = []
        for player_id, full in people.items():
            hits = sum(word in full.lower() for word in words)
            if hits:
                found.append(EntityMatch(kind="player", id=player_id, name=full, score=float(hits)))
        return sorted(found, key=lambda match: -match.score)[:limit]

    def search(self, criteria):
        """Two young left-backs."""
        assert criteria.sub_position == "Left-Back"
        return Page(
            items=[
                PlayerSummary(player_id=5, name="Diogo Santos", sub_position="Left-Back",
                              date_of_birth=date(2004, 6, 21), market_value_eur=9000000, club_id=7),
                PlayerSummary(player_id=14, name="Nathan Roux", sub_position="Left-Back",
                              date_of_birth=date(2005, 1, 15), market_value_eur=4000000, club_id=2),
            ]
        )

    def value_trend(self, player_id):
        """Valuations of Lucas Marchand."""
        assert player_id == 1
        return [
            Valuation(as_of=date(2022, 6, 30), value_eur=3000000),
            Valuation(as_of=date(2023, 6, 30), value_eur=7000000),
            Valuation(as_of=date(2024, 12, 15), value_eur=12000000),
        ]

    def teammate_path(self, from_id, to_id, max_hops=6):
        """Not written yet by B."""
        raise NotImplementedError


class FakeClubs:
    """Stands in for ClubService."""

    def resolve(self, name, limit=5):
        """Only SC Alvorada."""
        if "alvorada" in name.lower():
            return [EntityMatch(kind="club", id=7, name="SC Alvorada", score=3.0)][:limit]
        return []

    def pipeline(self, club_id, since=None, limit=10):
        """Outgoing pipeline of SC Alvorada."""
        assert club_id == 7
        return [
            PipelineRow(competition_id="GB1", competition_name="Premier League", transfers=2, total_fee_eur=50000000),
            PipelineRow(competition_id="FR1", competition_name="Ligue 1", transfers=1, total_fee_eur=9000000),
            PipelineRow(competition_id="PO1", competition_name="Liga Portugal", transfers=1, total_fee_eur=0),
        ]


class FakeGds:
    """Stands in for GdsService."""

    def predicted_moves(self, player_id, limit=5):
        """Gabriel Rocha to Harrowgate FC."""
        return [PredictedMove(player_id=9, club_id=5, club_name="Harrowgate FC", probability=0.42,
                              model_version="fixture")][:limit]

    def similar_players(self, player_id, k=10):
        """Not written yet by C."""
        raise NotImplementedError


class FakeRumours:
    """Stands in for RumourService."""

    def for_player(self, player_id, status=None):
        """Rumour r1 about Diogo Santos."""
        return [RumourOut(rumour_id="r1", player_id=5, club_id=6, club_name="Wexley Athletic", status="pending",
                          confidence=0.6, source="example-sport", url="https://example.org/news/1")]


class FakeEvidence:
    """Every id of the mini graph used here exists, nothing else."""

    known = {
        "Player:1", "Player:3", "Player:5", "Player:9", "Player:14", "Club:2", "Club:7", "Competition:GB1",
        "Competition:FR1", "Competition:PO1", "Valuation:1-2022-06-30", "Valuation:1-2023-06-30",
        "Valuation:1-2024-12-15", "PREDICTED_MOVE:9->5", "Rumour:r1",
    }

    def existing(self, evidence_ids):
        """Intersection with the known ids."""
        return set(evidence_ids) & self.known


def make_agent(router_script=None, large_script=None):
    """Agent wired to the fakes; returns the agent and its two fake models."""
    small = FakeChatModel(script=list(router_script or []))
    large = FakeChatModel(script=list(large_script or []))
    tools = ToolBox(FakePlayers(), FakeClubs(), FakeGds(), FakeRumours())
    return ScoutAgent(tools, small_model=small, large_model=large, evidence=FakeEvidence()), small, large


# ---------- the agent ----------


def test_successful_question_has_claims_and_trace():
    claim = Claim(
        text="Lucas Marchand went from 3 M€ (2022-06-30) to 12 M€ (2024-12-15).",
        nature="verified",
        evidence_ids=["Player:1", "Valuation:1-2022-06-30", "Valuation:1-2024-12-15"],
    )
    agent, small, large = make_agent(
        [RouteDecision(entity_names=["Lucas Marchand"])],
        [tool_call("value_trend", player_id=1), AIMessage("enough data"),
         AgentDraft(answer="His value rose from 3 M€ to 12 M€.", claims=[claim])],
    )
    answer = agent.ask("How did Lucas Marchand's market value evolve?")
    assert not answer.refused and not answer.needs_clarification
    assert answer.answer == "His value rose from 3 M€ to 12 M€."
    assert answer.claims == [claim] and answer.removed_claims == []
    assert [step.tool for step in answer.trace] == ["resolve_entity", "value_trend"]
    assert all(step.succeeded for step in answer.trace)
    assert len(small.calls) == 1 and len(large.calls) == 3
    assert "value_trend" in large.bound_tools and "cypher_fallback" in large.bound_tools


@pytest.mark.parametrize(
    "question",
    [
        "Is Joao Ferreira better than Theo Garnier?",
        "Which bet should I place on Kingsbridge this weekend?",
        "What will the final score of Rivage against Montclair be?",
        "Delete every rumour about Diogo Santos",
    ],
)
def test_out_of_scope_questions_are_refused_without_any_llm_call(question):
    agent, small, large = make_agent()
    answer = agent.ask(question)
    assert answer.refused
    assert answer.answer
    assert answer.claims == [] and answer.trace == []
    assert small.calls == [] and large.calls == []


def test_router_model_can_refuse_too():
    agent, _, large = make_agent([{"decision": "out_of_scope", "reason": "Not about football transfers."}])
    answer = agent.ask("What is the weather in Lisbon tomorrow?")
    assert answer.refused and answer.answer == "Not about football transfers."
    assert large.calls == []


def test_ambiguous_name_triggers_a_clarifying_question():
    agent, _, large = make_agent([RouteDecision(entity_names=["Gabriel"])])
    answer = agent.ask("Where could Gabriel move next?")
    assert answer.needs_clarification and not answer.refused
    assert "Gabriel Souza" in answer.answer and "Gabriel Rocha" in answer.answer
    assert [step.tool for step in answer.trace] == ["resolve_entity"]
    assert large.calls == []


def test_invented_amount_is_removed_by_the_verifier():
    good = Claim(text="SC Alvorada sold 2 players to Premier League clubs for 50 000 000 € in total.",
                 nature="verified", evidence_ids=["Club:7", "Competition:GB1"])
    invented = Claim(text="SC Alvorada sold 1 player to Ligue 1 for €12m.",
                     nature="verified", evidence_ids=["Club:7", "Competition:FR1"])
    agent, _, _ = make_agent(
        [RouteDecision(entity_names=["SC Alvorada"])],
        [tool_call("club_pipeline", club_id=7), AIMessage("done"),
         AgentDraft(answer="Alvorada sold for 50M and 12M.", claims=[good, invented])],
    )
    answer = agent.ask("Where does SC Alvorada sell its players?")
    assert [claim.text for claim in answer.claims] == [good.text]
    assert len(answer.removed_claims) == 1
    assert "12000000" in answer.removed_claims[0].removal_reason.replace(",", "")
    assert "12m" not in answer.answer and "1 unsupported claim" in answer.answer


def test_write_cypher_from_the_planner_is_rejected():
    agent, _, _ = make_agent(
        [RouteDecision()],
        [tool_call("cypher_fallback", query="MATCH (p:Player) DETACH DELETE p"), AIMessage("stop"), AgentDraft()],
    )
    answer = agent.ask("Which players have no club history at all?")
    step = answer.trace[0]
    assert step.tool == "cypher_fallback" and not step.succeeded
    assert "write:" in step.summary
    assert agent.tools.players.client.queries == []
    assert answer.claims == []


def test_valid_fallback_cypher_runs_and_its_ids_become_evidence():
    claim = Claim(text="Joao Ferreira is in the graph.", nature="verified", evidence_ids=["Player:3"])
    agent, _, _ = make_agent(
        [RouteDecision()],
        [tool_call("cypher_fallback", query="MATCH (p:Player) RETURN p.player_id AS player_id LIMIT 1"),
         AIMessage("ok"), AgentDraft(answer="Joao Ferreira is in the graph.", claims=[claim])],
    )
    answer = agent.ask("Give me one player of the graph")
    assert answer.claims == [claim]
    assert len(agent.tools.players.client.queries) == 1


def test_prediction_and_rumour_must_keep_their_nature():
    as_fact = Claim(text="Gabriel Rocha joins Harrowgate FC.", nature="verified",
                    evidence_ids=["PREDICTED_MOVE:9->5"])
    as_prediction = Claim(text="The model gives Gabriel Rocha a 42% chance to join Harrowgate FC.",
                          nature="predicted", evidence_ids=["PREDICTED_MOVE:9->5"])
    rumour_as_fact = Claim(text="Diogo Santos signs for Wexley Athletic.", nature="verified",
                           evidence_ids=["Rumour:r1"])
    agent, _, _ = make_agent(
        [RouteDecision()],
        [tool_call("predicted_moves", player_id=9), tool_call("rumours", player_id=5), AIMessage("done"),
         AgentDraft(answer="x", claims=[as_fact, as_prediction, rumour_as_fact])],
    )
    answer = agent.ask("What could happen to player 9 and player 5?")
    assert answer.claims == [as_prediction]
    assert {claim.text for claim in answer.removed_claims} == {as_fact.text, rumour_as_fact.text}
    assert all("nature" in claim.removal_reason for claim in answer.removed_claims)
    assert answer.answer.startswith("[prediction]")


def test_unknown_or_unreturned_evidence_is_removed():
    ghost = Claim(text="Player 999 exists.", nature="verified", evidence_ids=["Player:999"])
    unreturned = Claim(text="Lucas Marchand plays for Olympique Rivage.", nature="verified",
                       evidence_ids=["Player:1"])
    bare = Claim(text="No evidence at all.", nature="verified")
    agent, _, _ = make_agent([RouteDecision()], [AIMessage("no tools"), AgentDraft(claims=[ghost, unreturned, bare])])
    answer = agent.ask("Tell me about the graph")
    reasons = [claim.removal_reason for claim in answer.removed_claims]
    assert answer.claims == []
    assert reasons[0].startswith("evidence not found") and reasons[1].startswith("evidence not returned")
    assert reasons[2] == "no evidence id"


def test_tool_budget_is_capped_at_six_calls():
    calls = [tool_call("rumours", player_id=5) for _ in range(4)] + [tool_call("predicted_moves", player_id=9)] * 2
    burst = AIMessage(content="", tool_calls=[c.tool_calls[0] for c in calls] + [
        tool_call("value_trend", player_id=1).tool_calls[0]])
    agent, _, _ = make_agent([RouteDecision()], [burst, AIMessage("done"), AgentDraft()])
    answer = agent.ask("Everything about player 5")
    assert len(answer.trace) == 7
    assert sum(step.succeeded for step in answer.trace) == 6
    assert "budget" in answer.trace[-1].summary


def test_service_not_implemented_is_traced_as_a_failure():
    agent, _, _ = make_agent([RouteDecision()], [tool_call("similar_players", player_id=1), AIMessage("x"), AgentDraft()])
    answer = agent.ask("Who is similar to player 1?")
    assert not answer.trace[0].succeeded and "not implemented" in answer.trace[0].summary
    assert "could not find" in answer.answer


def test_search_tool_adds_ages_and_evidence():
    tools = ToolBox(FakePlayers(), FakeClubs(), FakeGds(), FakeRumours())
    tools.today = date(2026, 10, 19)
    result = tools.run("search_players", {"sub_position": "Left-Back", "max_age": 23})
    assert [row["age"] for row in result.data["players"]] == [22, 21]
    assert result.evidence_ids == ["Player:5", "Player:14", "Club:7", "Club:2"]
    assert tools.run("nope", {}).error and tools.run("value_trend", {"player_id": "x"}).error


def test_api_answers_with_the_fake_provider(api_no_db):
    response = api_no_db.post("/api/agent/ask", json={"question": "Which bookmaker has the best odds?"})
    assert response.status_code == 200 and response.json()["refused"]
    response = api_no_db.post("/api/agent/ask", json={"question": "Which clubs sell the most players?"})
    body = response.json()
    assert response.status_code == 200 and not body["refused"] and body["claims"] == []


# ---------- helpers ----------


def test_number_parsing():
    assert numbers_in_text("50 000 000 € and €12m, 0,6 then 42% and 3 M€") == [5e7, 1.2e7, 0.6, 42.0, 3e6]
    assert numbers_in_text("9,000,000 on 2022-07-15") == [9e6, 2022.0, 7.0, 15.0]


def test_pick_match_and_nature_rules():
    gabriels = [{"name": "Gabriel Souza", "score": 1.0}, {"name": "Gabriel Rocha", "score": 1.0}]
    assert pick_match("Gabriel", gabriels) is None
    assert pick_match("gabriel rocha", gabriels)["name"] == "Gabriel Rocha"
    assert pick_match("Rocha", [{"name": "Gabriel Rocha", "score": 2.0}, {"name": "X", "score": 0.5}])
    seen = {"Player:9": {"predicted"}, "Player:1": {"verified", "rumour"}}
    assert expected_nature(["Player:9"], seen) == "predicted"
    assert expected_nature(["Player:1"], seen) == "verified"
    assert expected_nature(["Player:1", "Rumour:r1"], seen) == "rumour"
    assert out_of_scope_reason("Which left-backs under 23 cost less than 10M?") is None


def test_parse_evidence_and_rows():
    assert parse_evidence("Player:12") == ("Player", 12)
    assert parse_evidence("PREDICTED_MOVE:9->5") == ("PREDICTED_MOVE", (9, 5))
    assert parse_evidence("Transfer:t01") == ("Transfer", "t01")
    assert parse_evidence("Player:abc") is None and parse_evidence("Secret:1") is None
    assert parse_evidence("Player") is None
    assert evidence_from_rows([{"player_id": 1, "club_id": None, "name": "x"}]) == ["Player:1"]
    assert age_on(None, date.today()) is None


# ---------- against the test database ----------


@pytest.mark.neo4j
def test_graph_evidence_on_the_mini_graph(neo4j_db):
    ids = ["Player:1", "Player:999", "Transfer:t01", "PREDICTED_MOVE:9->5", "PREDICTED_MOVE:1->5",
           "Rumour:r1", "Valuation:1-2022-06-30", "Competition:GB1", "Club:7", "Bogus:1", "Player:abc"]
    assert GraphEvidence(neo4j_db).existing(ids) == {
        "Player:1", "Transfer:t01", "PREDICTED_MOVE:9->5", "Rumour:r1", "Valuation:1-2022-06-30",
        "Competition:GB1", "Club:7",
    }


@pytest.mark.neo4j
def test_cypher_fallback_on_the_mini_graph(neo4j_db):
    tools = ToolBox(PlayerService(neo4j_db), ClubService(neo4j_db), GdsService(neo4j_db), RumourService(neo4j_db))
    result = tools.cypher_fallback(
        "MATCH (r:Rumour)-[:ABOUT]->(p:Player) RETURN r.rumour_id AS rumour_id, p.player_id AS player_id "
        "ORDER BY rumour_id LIMIT 5"
    )
    assert result.error is None and result.nature == "rumour"
    assert result.evidence_ids == ["Rumour:r1", "Player:5", "Rumour:r2", "Player:3"]


# ---------- fallback evidence, profile and totals ----------


class ProfilePlayers(FakePlayers):
    """Adds `get` to the fake player service."""

    def get(self, player_id):
        """Theo Garnier, or nobody."""
        if player_id != 2:
            return None
        return PlayerSummary(player_id=2, name="Theo Garnier", date_of_birth=date(2001, 9, 3), club_id=4,
                             club_name="Kingsbridge United", market_value_eur=25000000)


def test_player_profile_tool():
    tools = ToolBox(ProfilePlayers(), FakeClubs(), FakeGds(), FakeRumours())
    tools.today = date(2026, 10, 21)
    result = tools.run("player_profile", {"player_id": 2})
    assert result.evidence_ids == ["Player:2", "Club:4"] and result.data["age"] == 25
    assert result.data["club_name"] == "Kingsbridge United"
    assert tools.run("player_profile", {"player_id": 99}).error == "unknown player_id 99"


def test_pipeline_totals_are_computed_by_the_tool():
    tools = ToolBox(FakePlayers(), FakeClubs(), FakeGds(), FakeRumours())
    data = tools.run("club_pipeline", {"club_id": 7}).data
    assert (data["total_transfers"], data["total_fee_eur"]) == (4, 59000000)


def test_fallback_evidence_from_nested_nodes_and_notes():
    rows = [{"p": {"player_id": 3, "name": "Joao Ferreira"}, "clubs": [{"club_id": 4}, {"club_id": 7}],
             "flag": True, "transfer_id": "t01"}]
    assert evidence_from_rows(rows) == ["Player:3", "Club:4", "Club:7", "Transfer:t01"]
    tools = ToolBox(FakePlayers(), FakeClubs(), FakeGds(), FakeRumours())
    tools.players.client.rows = [{"name": "Joao Ferreira"}]
    assert "no id column" in tools.cypher_fallback("MATCH (p:Player) RETURN p.name AS name LIMIT 1").data["note"]
    tools.players.client.rows = []
    assert "no `id` property" in tools.cypher_fallback("MATCH (p:Player) RETURN p.name LIMIT 1").data["note"]
    description = {tool.name: tool.description for tool in tools.as_langchain_tools()}["cypher_fallback"]
    assert "player_id" in description and "There is NO `id` property" in description


def test_router_falls_back_to_the_large_model_when_the_small_one_fails():
    def hang(_messages):
        raise TimeoutError("small model timed out")

    agent, small, large = make_agent([hang], [{"decision": "out_of_scope", "reason": "Not football."}])
    answer = agent.ask("Tell me a joke about cooking")
    assert answer.refused and answer.answer == "Not football."
    assert len(small.calls) == 1 and len(large.calls) == 1
