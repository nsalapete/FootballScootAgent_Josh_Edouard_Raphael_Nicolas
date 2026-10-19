# Decisions — role D (agent, rumours)

One line per choice that the shared rules did not settle.

- D1: no temperature is forced on Claude (provider default); `openai_compatible` uses `temperature=0` for cheaper, steadier models.
- D1: the `fake` provider is a scripted `FakeChatModel` (messages, `tool_call()` helpers, dicts for structured output); with an empty script it answers a fixed refusal text, so the API never calls a network model in tests.
- D1: no new settings: `get_chat_model` only reads the existing `LLM_*` fields of `app/config.py` (owned by B).
- D2: one trailing `;` is tolerated and removed; any other `;` means several statements and is rejected.
- D2: `LIMIT` must be an integer literal (a `$param` value cannot be checked statically), at most 100, after the last `RETURN` of every `UNION` branch.
- D2: procedures are allow-listed (full-text lookup, `db.labels`-like, GDS `stream`/`stats`); everything else under `CALL` is refused, and `dbms.*`, `gds.graph.*`, write/dynamic `apoc.*` are refused even as functions.
- D2: parenthesised label expressions `:(A|B)` and dynamic labels `:$(x)` are refused rather than parsed (false rejections are cheaper than false acceptances).
