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

## 4. Rumours (`app/services/rumours.py`, `scripts/import_articles.py`)

**Life cycle of a rumour.**

```
feed (RSS / GDELT JSON) -> new URL? -> small LLM extraction {player, club, quote, confidence}
   -> quote really in the article? -> both names resolve to exactly one node?
   -> (:Article)-[:REPORTS]->(:Rumour {status: 'pending'})-[:ABOUT]->(:Player), -[:TARGET]->(:Club)
   -> A's scripts/confirm_rumours.py: confirmed (+ CONFIRMED_BY the real Transfer) | contradicted | expired
```

- **Import** (`import_articles`, protected by `X-API-Key` on `POST /api/rumours/import`): URLs already stored as
  `Article.url` are skipped *before* any LLM call, so an article is never paid for twice. One small-model call per
  new article, structured output `ArticleExtraction`.
- **Three anti-hallucination checks**, each with a counter in `ImportReport`:
  1. the quote must appear in the article (ignoring case, accents, punctuation) → otherwise `rejected_quotes`;
  2. a name is kept only if exactly one of the top-5 full-text hits contains all its words ("Gabriel" matches two
     players → not resolved) → otherwise `unresolved_names`. A name is **counted, never guessed**;
  3. the rumour is created only between existing nodes (`MATCH` on ids, never `MERGE` of a player or club).
- **No duplicates.** A new article on a player/club pair that already has a `pending` rumour is attached to it
  (`REPORTS`); the rumour id is a hash of URL + player + club, so re-importing is idempotent. A's job dates the
  rumour from its earliest article.
- **Failures.** A failing feed is counted (`feeds_failed`); an article whose extraction fails is not stored
  (`extraction_errors`) so the next run retries it.
- **Reads.** `for_player` returns rumours with the newest article's source, URL and date, newest first, `LIMIT 100`;
  `nature` is always `rumour`, whatever the status.

**Source reliability (D5).** Displayed `confidence` = extraction confidence × source reliability, where
reliability = `(confirmed + 5 × prior) / (decided + 5)`, decided = confirmed + contradicted + expired, prior = global
confirmation rate (0.5 when nothing is decided). The raw values stay visible (`extraction_confidence`,
`source_reliability`). Why smoothing: a source with 1 confirmed rumour out of 1 is not 100 % reliable; a large
source converges to its own rate. Why at read time: it follows A's confirmation job with no stored score to sync.
Cost: two small aggregate queries per `for_player` call (one over the decided rumours, one per source cited).

| Alternative | Why rejected |
| --- | --- |
| Trust the LLM's player/club ids | it would invent ids; resolution against the graph is checkable |
| Fuzzy "best match" resolution | silently picks the wrong homonym; counting unresolved names is honest |
| Raw confirmation rate per source | 1/1 = 100 %; unstable for small sources |
| Storing reliability on `Article` | must be recomputed after every confirmation run |

## 5. Evaluation and guardrail demo (`docs/agent_eval.md`, `docs/guardrails.md`)

**Figures to know.** The evaluation has 40 questions on the mini graph: 30 answerable, 10 traps. It ran with
NanoGPT (minimax-m3.1-flash small, mimo-v2.6-pro large).

| | Run 1 | Run 2 |
| --- | --- | --- |
| Correct | 26 / 40 | **39 / 40** |
| Traps | 10 / 10 | 10 / 10 |
| Claims removed | 34 | 5 |
| Generated Cypher calls / rejected | 45 / 5 | 21 / 2 |
| Mean latency | 49 s | 40 s |
| Cost | $0.12 | $0.11 |

**What the evaluation taught us** (be ready to tell it as a story: it shows the guardrails doing their job):
1. NanoGPT models refuse LangChain's structured output (both `json_schema` and forced tool calls). We switched to a
   JSON schema in the prompt + Pydantic validation + one retry (`app/llm/client.structured`).
2. Run 1 failed every question that needed generated Cypher. The model guessed `p.id` instead of `player_id`, and
   when it found the data, its rows had no ids, so the verifier removed the claims. The verifier was right: no
   unverifiable claim reached the user, but the answer was incomplete. Giving the schema to the tool and asking for
   id columns fixed 12 of 13.
3. The small model hung on one question every time. The router now falls back to the large model.
4. While preparing the demo, real `curl` calls revealed a verifier false positive: driver `Date` objects were
   ignored, so a correct date was removed. Fixed in PR #30.

**Why a rejected output in the demo is good news.** In the demo, the validator rejects a generated query without
`LIMIT`, and the model fixes it. The verifier also forces the fee difference to be computed by the query instead of
the model. Three guardrails show up in one trace.

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
- *Can a confirmed rumour become a fact?* No: the fact is the Transfermarkt `Transfer`; the rumour only points to
  it with `CONFIRMED_BY`, and the agent still labels the rumour itself `rumour`.
- *What if the press names a player who is not in the graph?* The name is counted in `unresolved_names` and the
  rumour is not stored.
- *Why does the router use regex before the LLM?* Obvious refusals are free, deterministic and testable; the LLM
  handles the subtle cases.

## Session log

- Monday 19/10: D1 (LLM client, PR #1), D2 (Cypher validator, PR #2), D3 (agent core, PR #3) merged, CI green.
  Also removed stray remote branches `feat`, `fix`, `docs`, `test`, `chore`, `refactor` that blocked every
  `type/…` branch name for the whole team.
- Tuesday 20/10: D4 (rumour reads and import, PR #19), D5 (source reliability, PR #20) merged, CI green.
  Checked every agent tool against the real services of B and C on the mini graph: all answer correctly
  (`predicted_moves` of C still raises `NotImplementedError` and is traced as a failed call).
  Reviewed A's PRs #13–#16 (2 comments each: DuckDB pre-filter, Decimal NaN, loans in `SENDS_TO`, stale
  `PLAYED_WITH`, loan false positives, pruned transfers vs `CONFIRMED_BY`, confirmation window tolerance,
  pending rumours beyond the batch limit).
