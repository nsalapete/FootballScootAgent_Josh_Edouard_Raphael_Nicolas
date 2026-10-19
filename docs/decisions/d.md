# Decisions — role D (agent, rumours)

One line per choice that the shared rules did not settle.

- D1: no temperature is forced on Claude (provider default); `openai_compatible` uses `temperature=0` for cheaper, steadier models.
- D1: the `fake` provider is a scripted `FakeChatModel` (messages, `tool_call()` helpers, dicts for structured output); with an empty script it answers a fixed refusal text, so the API never calls a network model in tests.
- D1: no new settings: `get_chat_model` only reads the existing `LLM_*` fields of `app/config.py` (owned by B).
