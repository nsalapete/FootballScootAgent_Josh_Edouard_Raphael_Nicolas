"""LangGraph state graph of the scouting agent. Owner: D.

    START -> router --(out of scope / ambiguous name)--> END (refusal or clarifying question)
               |
               v
            planner <-> tools        (large model with bound tools, at most 6 tool calls)
               |
               v
            writer -> verifier -> END (structured `AgentAnswer`)

The router runs cheap deterministic rules first, then the small model. The verifier is deterministic: it never
trusts the LLM, it checks every claim against the graph and the tool results of the trace.
"""

import json
import operator
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.agent.evidence import GraphEvidence, number_is_supported, numbers_in_data, numbers_in_text
from app.agent.tools import ToolBox, ToolResult, failure
from app.llm.client import get_chat_model, structured
from app.models.agent import AgentAnswer, Claim, Nature, ToolCall

MAX_TOOL_CALLS = 6
MAX_PLANNER_TURNS = 8

# Deterministic out-of-scope rules (checked before any LLM call): (pattern, reason shown to the user).
OUT_OF_SCOPE_RULES = [
    (
        r"\b(bet|bets|betting|odds|bookmakers?|paris?|parier|cotes?)\b",
        "Betting advice is out of scope: the agent only reports transfer, valuation and network facts.",
    ),
    (
        r"\b(score|scoreline|who will win|qui va gagner|r[ée]sultat du match|final score)\b",
        "Match results and scores are out of scope: the graph holds transfers, not matches to come.",
    ),
    (
        r"\b(better|best player|worst|overrated|underrated|world[- ]class|meilleur joueur|pire|surcot[ée]|"
        + r"sous-cot[ée]|niveau|talented|is .+ good|est[- ]il bon|est bon)\b",
        "Judging a player's level is out of scope: the agent reports facts, predictions and rumours, not opinions.",
    ),
    (
        r"\b(delete|remove|drop|detach|insert|update|modify|supprim\w*|efface\w*|modifi\w*|ajoute\w*|"
        + r"mets? [àa] jour|set the|change the)\b",
        "Changing the graph is not allowed: the agent is read-only.",
    ),
]

ROUTER_PROMPT = """You route questions for a football transfer scouting assistant built on a Neo4j graph
(players, clubs, competitions, transfers, market valuations, teammate links, ML predictions, press rumours).
Answer `answer` for questions the graph can answer. Answer `out_of_scope` (with a short reason) for: judging a
player's level or talent, betting or odds, match scores or results, any request to change data, or anything
unrelated to football transfers. List in `entity_names` every player or club name exactly as written."""

PLANNER_PROMPT = """You are a football scouting analyst. Answer only from tool results, never from memory.
Use the parameterised tools first; call `cypher_fallback` only when no other tool fits (read-only, LIMIT <= 100).
Use at most {max_calls} tool calls. Known entities: {entities}. Stop calling tools once you have enough data.
Any number you will state must appear in a tool result: let the tools or the query compute totals and counts."""

WRITER_PROMPT = """Write the final answer to the user's question from the tool results below, and nothing else.
Split it into short claims. For each claim:
- `nature` is `verified` for graph facts, `predicted` for results of `predicted_moves` (ML hypotheses),
  `rumour` for results of `rumours` (press, unconfirmed unless the status says confirmed). Never present a
  prediction or a rumour as a fact.
- `evidence_ids` are copied from the `evidence_ids` of the tool results (form `Label:id`).
- copy every number exactly as it appears in the tool results.
If the results do not answer the question, say so in one claim-free sentence."""


class RouteDecision(BaseModel):
    """Structured output of the router."""

    decision: Literal["answer", "out_of_scope"] = "answer"
    reason: str = ""
    entity_names: list[str] = Field(default_factory=list)


class AgentDraft(BaseModel):
    """Structured output of the writer, before verification."""

    answer: str = ""
    claims: list[Claim] = Field(default_factory=list)


class AgentState(TypedDict, total=False):
    """State shared by the nodes. `trace` and `results` accumulate across nodes."""

    question: str
    entities: list[dict[str, Any]]
    messages: list[BaseMessage]
    tool_calls: int
    planner_turns: int
    results: Annotated[list[ToolResult], operator.add]
    trace: Annotated[list[ToolCall], operator.add]
    draft: AgentDraft
    final: AgentAnswer


def out_of_scope_reason(question: str) -> str | None:
    """Reason to refuse `question` from the deterministic rules, None when they do not apply."""
    lowered = question.lower()
    for pattern, reason in OUT_OF_SCOPE_RULES:
        if re.search(pattern, lowered):
            return reason
    return None


def trace_step(result: ToolResult, arguments: dict[str, Any]) -> ToolCall:
    """Turn a tool result into one line of the trace."""
    if result.error:
        return ToolCall(tool=result.tool, arguments=arguments, succeeded=False, summary=result.error)
    summary = f"{result.nature}: {len(result.evidence_ids)} evidence id(s)"
    return ToolCall(tool=result.tool, arguments=arguments, succeeded=True, summary=summary)


def expected_nature(evidence_ids: list[str], seen: dict[str, set[str]]) -> Nature:
    """Nature a claim must carry given where its evidence came from: rumour > predicted > verified."""
    natures: set[str] = set()
    for evidence in evidence_ids:
        label = evidence.split(":", 1)[0]
        if label in ("Rumour", "Article"):
            natures.add("rumour")
        elif label.startswith("PREDICTED_"):
            natures.add("predicted")
        elif "verified" not in seen.get(evidence, set()):
            natures |= seen.get(evidence, set())
    if "rumour" in natures:
        return "rumour"
    if "predicted" in natures:
        return "predicted"
    return "verified"


class ScoutAgent:
    """Router, planner, tools, writer and evidence verifier wired as a LangGraph graph."""

    def __init__(self, tools: ToolBox, small_model=None, large_model=None, evidence: GraphEvidence | None = None):
        self.tools = tools
        self.small_model = small_model
        self.large_model = large_model
        self.evidence = evidence
        self.graph = self._build()

    # ---------- public ----------

    def ask(self, question: str) -> AgentAnswer:
        """Answer `question`; every claim carries its nature and evidence, and the trace lists each tool call."""
        state = self.graph.invoke(
            {"question": question, "tool_calls": 0, "planner_turns": 0, "results": [], "trace": []}
        )
        return state["final"]

    # ---------- wiring ----------

    def _build(self):
        """Compile the state graph."""
        graph = StateGraph(AgentState)
        graph.add_node("router", self._router)
        graph.add_node("planner", self._planner)
        graph.add_node("tools", self._tools)
        graph.add_node("writer", self._writer)
        graph.add_node("verifier", self._verifier)
        graph.add_edge(START, "router")
        graph.add_conditional_edges("router", lambda state: END if "final" in state else "planner")
        graph.add_conditional_edges("planner", self._after_planner)
        graph.add_edge("tools", "planner")
        graph.add_edge("writer", "verifier")
        graph.add_edge("verifier", END)
        return graph.compile()

    def _small(self):
        """Small model (routing), built lazily so that tests can inject one."""
        if self.small_model is None:
            self.small_model = get_chat_model("small")
        return self.small_model

    def _large(self):
        """Large model (planning and writing), built lazily."""
        if self.large_model is None:
            self.large_model = get_chat_model("large")
        return self.large_model

    def _graph_evidence(self) -> GraphEvidence:
        """Evidence checker bound to the database of the services."""
        if self.evidence is None:
            self.evidence = GraphEvidence(self.tools.players.client)
        return self.evidence

    # ---------- nodes ----------

    def _router(self, state: AgentState) -> dict[str, Any]:
        """Refuse out-of-scope questions, ask for precision on ambiguous names, resolve the others."""
        question = state["question"]
        names: list[str] = []
        reason = out_of_scope_reason(question)
        if reason is None:
            decision = self._route(question)
            if decision.decision == "out_of_scope":
                reason = decision.reason or "This question is outside the scope of the transfer graph."
            names = decision.entity_names
        if reason is not None:
            return {"final": AgentAnswer(answer=reason, refused=True)}

        entities, results, trace, questions = [], [], [], []
        for name in names[:4]:
            result = self.tools.run("resolve_entity", {"name": name})
            results.append(result)
            trace.append(trace_step(result, {"name": name}))
            if result.error or not result.data:
                continue
            chosen = pick_match(name, result.data)
            if chosen is None:
                options = ", ".join(f"{m['name']} ({m['kind']} {m['id']})" for m in result.data[:5])
                questions.append(f"Which '{name}' do you mean: {options}?")
            else:
                entities.append(chosen)
        if questions:
            final = AgentAnswer(answer=" ".join(questions), trace=trace, needs_clarification=True)
            return {"final": final, "results": results, "trace": trace}
        return {"entities": entities, "results": results, "trace": trace}

    def _route(self, question: str) -> RouteDecision:
        """Routing decision of the small model; the large model takes over if the small one fails or times out
        (the evaluation found a question on which the small NanoGPT model hangs every time)."""
        messages = [SystemMessage(ROUTER_PROMPT), HumanMessage(question)]
        try:
            return structured(self._small(), RouteDecision).invoke(messages)
        except Exception:  # pylint: disable=broad-exception-caught
            return structured(self._large(), RouteDecision).invoke(messages)

    def _planner(self, state: AgentState) -> dict[str, Any]:
        """Let the large model choose the next tool calls."""
        messages = state.get("messages") or [
            SystemMessage(
                PLANNER_PROMPT.format(
                    max_calls=MAX_TOOL_CALLS, entities=json.dumps(state.get("entities", []), ensure_ascii=False)
                )
            ),
            HumanMessage(state["question"]),
        ]
        model = self._large().bind_tools(self.tools.as_langchain_tools())
        reply = model.invoke(messages)
        return {"messages": messages + [reply], "planner_turns": state.get("planner_turns", 0) + 1}

    def _after_planner(self, state: AgentState) -> str:
        """Go to the tools while the model asks for some and the budget allows it, else write."""
        last = state["messages"][-1]
        wants_tools = isinstance(last, AIMessage) and bool(last.tool_calls)
        within_budget = state.get("tool_calls", 0) < MAX_TOOL_CALLS
        if wants_tools and within_budget and state.get("planner_turns", 0) < MAX_PLANNER_TURNS:
            return "tools"
        return "writer"

    def _tools(self, state: AgentState) -> dict[str, Any]:
        """Run the requested tool calls (beyond the budget they are skipped and traced as such)."""
        used = state.get("tool_calls", 0)
        replies, results, trace = [], [], []
        for call in state["messages"][-1].tool_calls:
            arguments = call.get("args", {})
            if used >= MAX_TOOL_CALLS:
                result = failure(call["name"], f"tool budget of {MAX_TOOL_CALLS} calls exhausted")
            else:
                result = self.tools.run(call["name"], arguments)
                used += 1
                results.append(result)
            trace.append(trace_step(result, arguments))
            replies.append(ToolMessage(content=result.to_message(), tool_call_id=call["id"], name=call["name"]))
        return {"messages": state["messages"] + replies, "tool_calls": used, "results": results, "trace": trace}

    def _writer(self, state: AgentState) -> dict[str, Any]:
        """Turn the tool results into a structured draft."""
        results = [result.model_dump() for result in state.get("results", []) if not result.error]
        payload = json.dumps(
            {"question": state["question"], "entities": state.get("entities", []), "tool_results": results},
            default=str,
            ensure_ascii=False,
        )
        draft = structured(self._large(), AgentDraft).invoke(
            [SystemMessage(WRITER_PROMPT), HumanMessage(payload[:20000])]
        )
        return {"draft": draft}

    def _verifier(self, state: AgentState) -> dict[str, Any]:
        """Keep a claim only if its evidence exists, was returned by a tool, its numbers come from the
        tool results (or the question) and its nature matches the tools its evidence came from."""
        draft: AgentDraft = state["draft"]
        seen, known_numbers = collect_support(state)
        cited = {evidence for claim in draft.claims for evidence in claim.evidence_ids}
        existing = self._graph_evidence().existing(cited) if cited else set()

        kept, removed = [], []
        for claim in draft.claims:
            reason = claim_problem(claim, seen, existing, known_numbers)
            if reason is None:
                kept.append(claim)
            else:
                removed.append(claim.model_copy(update={"removal_reason": reason}))
        answer = draft.answer.strip()
        if removed or not answer:
            answer = render_answer(kept, removed)
        final = AgentAnswer(answer=answer, claims=kept, removed_claims=removed, trace=state.get("trace", []))
        return {"final": final}


def collect_support(state: AgentState) -> tuple[dict[str, set[str]], set[float]]:
    """Evidence ids returned by successful tools (with the natures of those tools) and every known number."""
    results = [result for result in state.get("results", []) if not result.error]
    seen: dict[str, set[str]] = {}
    for result in results:
        for evidence in result.evidence_ids:
            seen.setdefault(evidence, set()).add(result.nature)
    numbers = numbers_in_data([result.data for result in results]) | set(numbers_in_text(state["question"]))
    return seen, numbers


def pick_match(name: str, matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The match a name refers to, or None when several candidates are too close to choose."""
    exact = [match for match in matches if match["name"].casefold() == name.strip().casefold()]
    if len(exact) == 1:
        return exact[0]
    top = matches[0]
    close = [match for match in matches[1:] if match["score"] >= 0.75 * top["score"]]
    return None if close or len(exact) > 1 else top


def claim_problem(claim: Claim, seen: dict[str, set[str]], existing: set[str], numbers: set[float]) -> str | None:
    """Why a claim must be removed, or None when it is supported."""
    if not claim.evidence_ids:
        return "no evidence id"
    missing = [evidence for evidence in claim.evidence_ids if evidence not in existing]
    if missing:
        return f"evidence not found in the graph: {', '.join(missing)}"
    unseen = [evidence for evidence in claim.evidence_ids if evidence not in seen]
    if unseen:
        return f"evidence not returned by any tool: {', '.join(unseen)}"
    unsupported = [value for value in numbers_in_text(claim.text) if not number_is_supported(value, numbers)]
    if unsupported:
        return f"number(s) not found in any tool result: {', '.join(format_number(value) for value in unsupported)}"
    expected = expected_nature(claim.evidence_ids, seen)
    if claim.nature != expected:
        return f"nature is {claim.nature!r} but the evidence comes from {expected!r} sources"
    return None


def format_number(value: float) -> str:
    """`12000000.0` -> `12,000,000`, `0.42` -> `0.42`."""
    return f"{value:,.0f}" if value.is_integer() else f"{value:g}"


def render_answer(kept: list[Claim], removed: list[Claim]) -> str:
    """Rebuild the answer from the verified claims only, labelling predictions and rumours."""
    labels = {"verified": "", "predicted": "[prediction] ", "rumour": "[rumour] "}
    lines = [f"{labels[claim.nature]}{claim.text}" for claim in kept]
    if not lines:
        lines.append("I could not find verifiable information in the graph to answer this question.")
    if removed:
        lines.append(f"({len(removed)} unsupported claim(s) removed by the verifier.)")
    return " ".join(lines)
