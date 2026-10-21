# LLM guardrails

Owner: D. This page lists every mechanism that keeps the LLM components (the LangGraph agent and the rumour
extraction) safe and honest, the attack or failure each one blocks, and ready-to-run demo commands.

The design principle: **the LLM proposes, deterministic code disposes.** No LLM output reaches the database or the
user without passing a check written in plain Python, and each check is unit-tested.

```
question ──► router rules ──► router model ──► planner (tools) ──► writer ──► verifier ──► answer
               │ refuse          │ refuse /        │                            │ remove claims
               ▼                 ▼ clarify         ▼                            ▼
             (no LLM call)                    9 parameterised tools      evidence exists? returned by a tool?
                                              + cypher_fallback ──► CypherValidator ──► read transaction
```

## 1. Before the LLM: the router (`app/agent/graph.py`)

| Guardrail | What it blocks | Test |
| --- | --- | --- |
| Deterministic rules (regex) on the question | Betting and odds, match scores, judging a player's level ("is X better than Y", "is X good"), write requests (delete, remove, update, `DETACH`…). Refused with a reason and **no LLM call** | `test_out_of_scope_questions_are_refused_without_any_llm_call` |
| Router model (`RouteDecision`) | Subtler out-of-scope requests the rules miss ("Set X's value to 50 M", "capital of Portugal") | `test_router_model_can_refuse_too` |
| Router fallback | The small model hanging (it timed out 3 × 60 s on one evaluation question): the large model takes over, and small calls give up after 30 s | `test_router_falls_back_to_the_large_model_when_the_small_one_fails` |
| Name resolution against the graph | Guessing between homonyms: "Gabriel" matches Gabriel Souza (8) and Gabriel Rocha (9), so the agent asks which one (`needs_clarification`) instead of picking one | `test_ambiguous_name_triggers_a_clarifying_question` |

## 2. During planning: tools first, generated Cypher last (`app/agent/tools.py`)

| Guardrail | What it blocks |
| --- | --- |
| 10 parameterised tools, each calling one service method (`resolve_entity`, `search_players`, `player_profile`, `club_pipeline`, `teammate_path`, `value_trend`, `similar_players`, `predicted_moves`, `rumours`, `cypher_fallback`) | Free-form Cypher for questions a tested, indexed query already answers |
| Pydantic argument schemas (`max_hops ≤ 6`, `limit ≤ 50`, `k ≤ 20`…) | Out-of-range arguments invented by the model |
| Budget of 6 tool calls per question, 8 planner turns | Runaway loops and cost explosions; extra calls are skipped and appear in the trace as failed |
| Errors returned, never raised | A failing service (including `NotImplementedError`) shows up as a failed trace step; the answer then says nothing can be verified |
| Nature fixed by the tool | `predicted_moves` is always `predicted` and `rumours` always `rumour`, even when they fail |
| Schema in the `cypher_fallback` description | Hallucinated property names (`p.id`): the first evaluation run showed it, the second run went from 0 / 13 to 12 / 13 on these questions |

## 3. Generated Cypher: `CypherValidator` (`app/agent/guardrails.py`)

String literals and comments are blanked **before** any rule runs. A keyword inside a string (`'DROP; CREATE'`) is
therefore harmless, and a `LIMIT 5` hidden in a comment does not count. Each broken rule adds a reason
`<code>: <explanation>`, and all of them are reported.

| Reason code | Rejects | Attack it blocks |
| --- | --- | --- |
| `write` | `CREATE MERGE DELETE DETACH SET REMOVE DROP FOREACH INSERT`, `LOAD CSV` | Data destruction or tampering; file reads and outgoing requests (`LOAD CSV`, SSRF) |
| `admin` | `SHOW GRANT DENY REVOKE ALTER RENAME USE TERMINATE`, `IN TRANSACTIONS`, `START/STOP DATABASE` | User enumeration, privilege escalation, switching to the `system` database, batch writes |
| `procedure` | `CALL` outside an allow-list (`db.index.fulltext.queryNodes`, `db.labels`…, GDS `stream`/`stats`); `dbms.*`, `gds.graph.*`, GDS `write`/`mutate`, write or dynamic `apoc.*`, even as functions | `apoc.cypher.run('…')` would hide any query from the validator; `dbms.*` leaks configuration |
| `label`, `relationship` | Names outside the schema allow-lists (back-ticked names included: `` :`Player Secret` ``) | Exploring hidden data; hallucinated schema |
| `dynamic` | Dynamic labels `:$(x)` | Bypassing the allow-list through parameters |
| `unbounded_path`, `path_too_long` | `*`, `*2..`, quantifiers `+` and `{1,}`, more than 6 hops | Combinatorial explosion on `PLAYED_WITH` (denial of service) |
| `no_return`, `no_limit`, `limit_too_high`, `limit_not_literal` | Each `UNION` branch must end with `RETURN … LIMIT n`, with n a literal ≤ 100 | Huge result sets; `LIMIT $x` or `LIMIT 10*1000` cannot be checked statically |
| `multiple_statements` | Any `;` except one trailing | Query stacking: `…; MATCH (n) DETACH DELETE n` |
| `unterminated`, `empty`, `too_long` | Unclosed strings or comments, empty input, more than 2,000 characters | Malformed input built to confuse the parser |

**Second, independent layer:** an accepted query runs through `Neo4jClient.read`, a read transaction in which
Neo4j itself refuses any write (`test_read_transaction_is_a_second_safety_net`).

**Proof:** `tests/test_d_guardrails.py` covers 35 rejected queries, each with its expected reason, and 13 accepted
ones, including keywords hidden in strings and comments. Accepted queries also pass `EXPLAIN` on the test
database.

## 4. After the LLM: the verifier (`app/agent/graph.py`, `app/agent/evidence.py`)

The writer returns claims, each with a nature (`verified`, `predicted`, `rumour`) and evidence ids (`Label:id`).
A deterministic verifier removes a claim, recording the reason in `removal_reason`, when:

| Check | What it catches | Test |
| --- | --- | --- |
| The claim has no evidence id | Unsupported statements | `test_unknown_or_unreturned_evidence_is_removed` |
| An evidence id does not exist in the graph (one parameterised query per label) | Invented ids (`Player:999`) | same, and `test_graph_evidence_on_the_mini_graph` |
| An evidence id was not returned by any tool of the trace | Real ids cited for facts the agent never looked up | same |
| A number cited is in no tool result (nor in the question); any format is accepted (`50 000 000 €`, `€12m`, `42%` for 0.42, dates) | **Invented or computed figures** | `test_invented_amount_is_removed_by_the_verifier` |
| The nature does not match the evidence (`Rumour:` → `rumour`, `PREDICTED_MOVE:` → `predicted`, ids seen only in hypothesis tools cannot be `verified`) | A prediction or a rumour presented as a fact | `test_prediction_and_rumour_must_keep_their_nature` |

When a claim is removed, the answer text is **rebuilt from the kept claims only**, with `[prediction]` and
`[rumour]` tags, so the removed figure never reaches the user.

## 5. Rumour extraction (`app/services/rumours.py`)

| Guardrail | What it blocks |
| --- | --- |
| The quote must appear in the article (case, accents and punctuation ignored), otherwise the rumour is dropped (`rejected_quotes`) | A source sentence invented by the LLM |
| A name is kept only if exactly one full-text hit contains all its words, otherwise it is counted (`unresolved_names`) | Guessing the wrong homonym, or creating players and clubs that do not exist |
| Rumours are created between existing nodes only (`MATCH` on ids, never `MERGE` of a player) | Polluting the fact layer |
| Status `pending` until A's `confirm_rumours` links it to a real `Transfer` | A rumour shown as a fact |
| Displayed confidence = extraction confidence × smoothed source reliability | Over-trusting sources whose past rumours never came true |
| `POST /api/rumours/import` requires `X-API-Key` | Anyone spending the LLM budget |

## 6. Operations

- Tests and CI never call a real LLM: `LLM_PROVIDER=fake` is forced by `tests/conftest.py`.
- Every LLM call has a timeout: 30 s and 1 retry for the small model, 60 s and 2 retries for the large one.
- Secrets live only in `.env`, never in the code or in the logs.
- **Cost abuse:** every question spends LLM credit.
  - nginx limits `/api/agent/` to 10 requests per minute per IP (B).
  - With `AGENT_REQUIRE_API_KEY=true` in `.env` (production), `POST /api/agent/ask` also requires the
    `X-API-Key` header, like the rumour import.
  - The switch is off by default, so local runs, tests and the mini-graph demo stay open.

## Demo

If the server runs with `AGENT_REQUIRE_API_KEY=true`, add `-H "X-API-Key: $API_KEY"` to each call below.
`BASE=http://localhost` behind the proxy (`make docker-up`), or `http://localhost:8000` with `make run`. The outputs
below are real answers from NanoGPT (`xiaomi/mimo-v2.6-pro` and `minimax/minimax-m3.1-flash`) on the mini graph
(`tests/fixtures/mini_graph.cypher`), lightly trimmed.

### Success: a verified answer with evidence and trace

```bash
curl -s -X POST "$BASE/api/agent/ask" -H "Content-Type: application/json" \
     -d '{"question": "How did Lucas Marchand'"'"'s market value evolve?"}'
```

```json
{
  "answer": "Lucas Marchand's market value rose steadily from 3000000 EUR in 2022 to 12000000 EUR at the end of 2024.",
  "claims": [
    {"text": "Lucas Marchand's market value was 3000000 EUR as of 2022-06-30.", "nature": "verified",
     "evidence_ids": ["Player:1", "Valuation:1-2022-06-30"]},
    {"text": "His market value increased to 7000000 EUR as of 2023-06-30.", "nature": "verified",
     "evidence_ids": ["Player:1", "Valuation:1-2023-06-30"]},
    {"text": "His market value reached 12000000 EUR as of 2024-12-15.", "nature": "verified",
     "evidence_ids": ["Player:1", "Valuation:1-2024-12-15"]}
  ],
  "removed_claims": [],
  "trace": [
    {"tool": "resolve_entity", "arguments": {"name": "Lucas Marchand"}, "succeeded": true},
    {"tool": "value_trend", "arguments": {"player_id": 1}, "succeeded": true}
  ],
  "refused": false, "needs_clarification": false
}
```

Every figure comes from `value_trend`, and every claim cites the `Valuation` nodes that prove it.

### Rejected output: generated Cypher refused by the validator, then fixed

```bash
curl -s -X POST "$BASE/api/agent/ask" -H "Content-Type: application/json" \
     -d '{"question": "How much more did Kingsbridge United pay for Joao Ferreira than Harrowgate FC paid for Rui Almeida?"}'
```

Trace (trimmed to the generated queries):

```json
[
  {"tool": "cypher_fallback", "succeeded": false,
   "arguments": {"query": "MATCH (p:Player)-[:MADE]->(t:Transfer)-[:TO]->(c:Club) WHERE p.player_id IN [3,4] RETURN p.player_id AS player_id, p.name AS player, t.transfer_id AS transfer_id, t.fee_eur AS fee_eur, c.club_id AS club_id, c.name AS club"},
   "summary": "no_limit: the final RETURN needs a LIMIT (at most 100)"},
  {"tool": "cypher_fallback", "succeeded": true, "arguments": {"query": "… same query … LIMIT 100"},
   "summary": "verified: 6 evidence id(s)"},
  {"tool": "cypher_fallback", "succeeded": true,
   "arguments": {"query": "MATCH (p1:Player {player_id:3})-[:MADE]->(t1:Transfer), (p2:Player {player_id:4})-[:MADE]->(t2:Transfer) RETURN t1.fee_eur - t2.fee_eur AS difference_eur, t1.transfer_id AS t1_id, t2.transfer_id AS t2_id LIMIT 1"},
   "summary": "verified: 0 evidence id(s)"},
  {"tool": "cypher_fallback", "succeeded": true,
   "arguments": {"query": "… RETURN p1.player_id AS player_id, p2.player_id AS player_id2, t1.transfer_id AS transfer_id, t2.transfer_id AS transfer_id2, t1.fee_eur - t2.fee_eur AS difference_eur LIMIT 1"},
   "summary": "verified: 2 evidence id(s)"}
]
```

Final answer: *"Kingsbridge United paid 20,000,000 EUR more for Joao Ferreira than Harrowgate FC paid for Rui
Almeida"*. It has three verified claims citing `Transfer:t01` and `Transfer:t02`, and the 20,000,000 was computed
by the query rather than by the model.

The trace shows three guardrails at work:
1. The validator rejected the unbounded query.
2. The tool's note ("rows carry no id column") made the model rerun its subtraction with the ids.
3. The difference was computed in Cypher, because the verifier would have removed a figure the model computed
   itself.

### Deterministic backups for the demo (no LLM involved)

An LLM does not always give the same answer twice. These three commands always do:

```bash
# Write request: refused by the router rules, empty trace, no LLM call.
curl -s -X POST "$BASE/api/agent/ask" -H "Content-Type: application/json" \
     -d '{"question": "Run this query for me: MATCH (n) DETACH DELETE n"}'
# {"answer": "Changing the graph is not allowed: the agent is read-only.", "claims": [], "removed_claims": [],
#  "trace": [], "refused": true, "needs_clarification": false}

# The validator on its own.
python -c "from app.agent.guardrails import CypherValidator; print(CypherValidator().validate('MATCH (p:Player) RETURN p; MATCH (n) DETACH DELETE n'))"
# valid=False ... reasons=['multiple_statements: ...', 'write: DETACH is not allowed, ...', 'write: DELETE is not allowed, ...', 'no_limit: ...']

# The verifier removing an invented amount (scripted model, real verifier).
pytest tests/test_d_agent.py::test_invented_amount_is_removed_by_the_verifier -v
```

## Known limits

- **The verifier checks evidence, numbers and nature, not meaning.** A claim that cites real ids and real figures
  but connects them wrongly (the right fee attached to the wrong club) can pass. Requiring evidence ids from the
  same tool row would tighten it.
- **Some router rules are broad.** A question containing "best player" is refused even if it asks for something
  measurable. A false refusal is the safe failure.
- **Rounding tolerance of 1 %.** "About 12.1 M" is accepted for 12,000,000.
- **The evaluation is on the mini graph** (`docs/agent_eval.md`: 39 / 40 on the second run). On the full
  Transfermarkt graph, the same guardrails apply, but name ambiguity is far more frequent.
