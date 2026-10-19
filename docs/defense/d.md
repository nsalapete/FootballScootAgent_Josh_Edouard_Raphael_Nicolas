# Defense sheet — role D (agent, guardrails, rumours)

What was built, why, what was rejected, and the questions to expect. Every line can be checked in the code
cited next to it. Updated after each session.

## 1. LLM client (`app/llm/client.py`)

**What.** `get_chat_model(size)` returns a LangChain chat model chosen by `LLM_PROVIDER`:
`anthropic` (`ChatAnthropic`), `openai_compatible` (`ChatOpenAI` with `base_url`, e.g. NanoGPT) or `fake`.
`small` is used for routing and extraction (cheap, frequent), `large` for planning and writing (needs reasoning).

**The fake model.** `FakeChatModel` replays a script: text, tool calls (`tool_call("value_trend", player_id=1)`),
dicts or Pydantic objects for structured output. It records every call. Tests and CI set `LLM_PROVIDER=fake`
(enforced by an autouse fixture in `tests/conftest.py`), so no test ever spends credit or needs the network.

| Alternative | Why rejected |
| --- | --- |
| LangChain's `GenericFakeChatModel` | cannot bind tools or return structured output, so the agent loop could not be tested |
| Mocking the HTTP API | brittle, provider-specific, and tests would depend on the SDK internals |
| Local model (Ollama) for tests | slow, non-deterministic, heavy for CI |

## 2. Cypher validator (`app/agent/guardrails.py`)

**Principle.** The LLM never writes Cypher in the normal path: the agent calls parameterised tools. Generated
Cypher exists only in `cypher_fallback`, and it must pass `CypherValidator.validate` **and** then runs in a read
transaction (`Neo4jClient.read`), which the server refuses to write in: two independent layers.

**How it reads a query.** It first blanks string literals and comments (`strip_literals`), then tokenizes.
So a keyword inside a string (`'DROP; CREATE'`) is harmless, and a `LIMIT 5` hidden in a comment does not count.

| Rule (reason code) | Attack it blocks |
| --- | --- |
| `write`: `CREATE MERGE DELETE DETACH SET REMOVE DROP FOREACH INSERT`, `LOAD CSV` | data destruction or tampering; `LOAD CSV` = file read / outgoing requests (SSRF) |
| `admin`: `SHOW GRANT DENY REVOKE ALTER RENAME USE TERMINATE`, `IN TRANSACTIONS`, `START/STOP DATABASE` | privilege escalation, user enumeration, switching to the `system` database, batch writes |
| `procedure`: `CALL` outside an allow-list; `dbms.*`, `gds.graph.*`, GDS `write`/`mutate`, write or dynamic `apoc.*` even as functions | `apoc.cypher.run('…')` would hide any query from the validator; `dbms.*` leaks config |
| `label`, `relationship`: names outside the schema allow-lists (back-ticked names included) | exploring hidden data, hallucinated schema producing silent empty results |
| `dynamic`: `:$(x)` labels | bypass of the allow-list through parameters |
| `unbounded_path`, `path_too_long`: `*`, `*2..`, `+`, `{1,}`, more than 6 hops | combinatorial explosion (DoS) on `PLAYED_WITH` |
| `no_return`, `no_limit`, `limit_too_high`, `limit_not_literal`: every `UNION` branch must end with `RETURN … LIMIT n`, `n ≤ 100` literal | huge result sets; `LIMIT $x` or `LIMIT 10*1000` cannot be checked statically |
| `multiple_statements`: any `;` except one trailing | query stacking (`…; MATCH (n) DETACH DELETE n`) |
| `unterminated`, `empty`, `too_long` (2000 chars) | malformed input built to confuse the parser |

**Design choice: allow-lists, and refuse when unsure.** Parenthesised label expressions `:(A|B)` are refused
instead of parsed. A false rejection only costs a retry with a simpler query; a false acceptance could cost the data.

**Proof.** `tests/test_d_guardrails.py`: 35 refused queries, each with its expected reason, 13 accepted ones
(including keywords inside strings and comments), `EXPLAIN` of accepted queries on the test database, and a write
attempt rejected by the read transaction itself.

**Cost.** Validation is pure Python on a ≤ 2000-character string: a few hundred microseconds, no database call.

## 3. Agent (`app/agent/graph.py`, `tools.py`, `evidence.py`)

```
START -> router --(refusal / ambiguous name)--> END
           |
        planner <-> tools      (large model, tools bound, ≤ 6 tool calls)
           |
        writer -> verifier -> END   (AgentAnswer: answer, claims, removed_claims, trace)
```

- **Router.** Deterministic regex rules first (betting, scores, level judgement such as "is X better than Y",
  write requests): refused with a reason and **zero LLM call**. Otherwise the small model decides
  (`RouteDecision`) and lists the names; each name is resolved with the full-text indexes. If no candidate matches
  exactly and a second one scores ≥ 75 % of the best ("Gabriel" → Gabriel Souza (8), Gabriel Rocha (9)), the agent
  asks which one (`needs_clarification`) instead of guessing.
- **Planner.** Large model with the 9 tools bound; at most 6 tool calls (extra calls are not executed and appear
  in the trace as "budget exhausted"), at most 8 planner turns.
- **Tools.** Each calls exactly one service method and returns a `ToolResult`: data, nature
  (`verified` / `predicted` / `rumour`) and evidence ids `Label:id`. `predicted_moves` is always `predicted`,
  `rumours` always `rumour`. Errors (including `NotImplementedError` of a service not written yet) come back as a
  failed trace step, never as an exception.
- **Writer.** Structured output `AgentDraft`: an answer split into claims, each with nature and evidence ids.
- **Verifier (deterministic, trusts nothing from the LLM).** A claim is removed (with `removal_reason`) when:
  1. it has no evidence id;
  2. an evidence id does not exist in the graph (`GraphEvidence.existing`, one parameterised query per label);
  3. an evidence id was not returned by any tool of the trace;
  4. a number it cites (`50 000 000 €`, `€12m`, `42%`, dates) is not in any tool result nor in the question
     (1 % tolerance for rounding, `42%` matches a probability `0.42`);
  5. its nature does not match its evidence: a `Rumour:` id forces `rumour`, a `PREDICTED_MOVE:` id forces
     `predicted`, an id seen only in rumour/prediction results cannot support a `verified` claim.

  If anything is removed, the answer text is rebuilt from the kept claims only, with `[prediction]` and
  `[rumour]` tags: the invented figure never reaches the user.

**Why parameterised tools first and generated Cypher only as a fallback.** Tools are tested, indexed, bounded and
written by the team; their results carry typed evidence ids. Generated Cypher is the riskiest path (writes,
hallucinated schema, unbounded traversals), so it is the last resort and goes through the validator.

**What happens if the LLM invents a figure?** The verifier does not find it in the tool results: the claim moves to
`removed_claims` with reason "number(s) not found in any tool result: 12,000,000" (test
`test_invented_amount_is_removed_by_the_verifier`).

**What happens if the LLM generates a write?** `cypher_fallback` returns an error with the reason
(`write: DELETE is not allowed…`), the trace step is `succeeded: false`, and the database is never called (test
`test_write_cypher_from_the_planner_is_rejected`). Even if the validator had a bug, the read transaction refuses.

**Cost of one question.** 1 small-model call (none if a rule refuses), 1 to 8 large-model calls (planner turns)
and 1 writer call; graph side: one full-text lookup per name, ≤ 6 service queries, one existence query per label
cited. Exact latency and cost: `docs/agent_eval.md` (Thursday).

| Alternative | Why rejected |
| --- | --- |
| Pure NL-to-Cypher | every question would go through the riskiest path; no typed evidence |
| LangGraph prebuilt `ToolNode` / ReAct agent | no control over the tool budget, the trace format or failures |
| LLM-as-judge verifier | costs another call and can hallucinate too; a deterministic check is auditable |

## Likely questions

- *Can a string literal fool the validator?* No: strings and comments are blanked before any rule runs
  (`test_accepted_queries` contains `'DROP; CREATE (x) DETACH DELETE'` inside a string).
- *Why refuse `LIMIT $limit`?* The validator cannot know the value statically; a literal is checkable.
- *Why max 6 hops?* The teammate graph has a small diameter; beyond 6 the number of paths explodes, and the
  fixture shows a 7-hop pair is correctly unreachable.
- *Is a computed fact (PageRank, similarity) a prediction?* No: it is derived from facts, so `verified`
  (computed). Only `PREDICTED_MOVE`/`PREDICTED_LINK` are predictions.
- *What if a service is not implemented yet?* The tool returns an error, the trace shows it, the answer says it
  could not find verifiable information.
- *Why does the router use regex before the LLM?* Obvious refusals are free, deterministic and testable; the LLM
  handles the subtle cases.

## Session log

- Monday 19/10: D1 (LLM client, PR #1), D2 (Cypher validator, PR #2), D3 (agent core, PR #3) merged, CI green.
  Also removed stray remote branches `feat`, `fix`, `docs`, `test`, `chore`, `refactor` that blocked every
  `type/…` branch name for the whole team.
