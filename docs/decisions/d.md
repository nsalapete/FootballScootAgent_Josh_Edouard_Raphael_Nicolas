# Decisions — role D (agent, rumours)

One line per choice that the shared rules did not settle.

- D1: no temperature is forced on Claude (provider default); `openai_compatible` uses `temperature=0` for cheaper, steadier models.
- D1: the `fake` provider is a scripted `FakeChatModel` (messages, `tool_call()` helpers, dicts for structured output); with an empty script it answers a fixed refusal text, so the API never calls a network model in tests.
- D1: no new settings: `get_chat_model` only reads the existing `LLM_*` fields of `app/config.py` (owned by B).
- D2: one trailing `;` is tolerated and removed; any other `;` means several statements and is rejected.
- D2: `LIMIT` must be an integer literal (a `$param` value cannot be checked statically), at most 100, after the last `RETURN` of every `UNION` branch.
- D2: procedures are allow-listed (full-text lookup, `db.labels`-like, GDS `stream`/`stats`); everything else under `CALL` is refused, and `dbms.*`, `gds.graph.*`, write/dynamic `apoc.*` are refused even as functions.
- D2: parenthesised label expressions `:(A|B)` and dynamic labels `:$(x)` are refused rather than parsed (false rejections are cheaper than false acceptances).
- D3: the router applies deterministic rules (betting, scores, level judgement, write requests) before the small model, so obvious refusals cost no LLM call.
- D3: a name is ambiguous when no candidate matches it exactly and a second candidate scores at least 75 % of the best one; the agent then asks which one (`needs_clarification`), it does not guess.
- D3: name resolution in the router is traced but does not count in the 6 planner tool calls.
- D3: the verifier also requires each evidence id to have been returned by a tool of the trace (existing in the graph is not enough).
- D3: numbers of the question are allowed in claims (e.g. "under 23"); ages are computed by `search_players` so they appear in tool results.
- D3: when the verifier removes a claim, the answer is rebuilt from kept claims only, with `[prediction]` / `[rumour]` tags, so the invented figure never reaches the user.
- D3: optional fields added to my own models: `Claim.removal_reason`, `AgentAnswer.needs_clarification`.
- D4: default feeds are two RSS transfer feeds (Guardian, BBC) and one GDELT DOC query; the source of an article is its domain, so the reliability of D5 is computed per domain.
- D4: a name resolves only if exactly one of the top 5 full-text hits contains every word of the extracted name; otherwise it is counted in `unresolved_names` and the rumour is dropped.
- D4: the extracted quote must appear in the article (case, accents and punctuation ignored), otherwise the rumour is dropped (`rejected_quotes`): the LLM cannot invent a source sentence.
- D4: a new article about a player/club pair that already has a `pending` rumour is attached to it (`REPORTS`) instead of creating a duplicate; A's confirmation job then dates the rumour from its earliest article.
- D4: an article whose extraction fails is not stored, so the next import retries it; articles without rumours are stored, so they are never paid for twice.
- D4: `RumourService.fetcher` and `.model` are attributes set in `__init__` (the constructor signature is frozen), replaced by fakes in tests.
- D5: displayed confidence = extraction confidence × source reliability, with reliability = (confirmed + 5 × prior) / (decided + 5), decided = confirmed + contradicted + expired; the prior is the global confirmation rate (0.5 while nothing is decided). Raw values stay visible as `extraction_confidence` and `source_reliability`.
- D5: reliability is computed at read time (two small aggregate queries per `for_player` call) rather than stored, so it follows A's confirmation job without any write-back.
- D6: structured output goes through `structured()`: native for Claude and the fake model, JSON schema in the prompt + Pydantic validation + one retry for OpenAI-compatible models (NanoGPT refuses both `json_schema` response formats and forced `tool_choice`).
- D6: the evaluation scores answers automatically (expected names, numbers in any format, forbidden names, claim nature for rumours and predictions); it reloads the test database with the mini graph before the run.
- D6: the cost is estimated from LangChain usage metadata and the NanoGPT public prices passed on the command line (`--price-small`, `--price-large`).
