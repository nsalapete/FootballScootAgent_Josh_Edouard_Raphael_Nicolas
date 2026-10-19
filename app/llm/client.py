"""Single entry point to the chat models. Owner: D.

`LLM_PROVIDER` selects the backend: `anthropic` (Claude API), `openai_compatible` (NanoGPT or any
OpenAI-compatible API, using `LLM_BASE_URL`) or `fake` (deterministic, no network: tests and CI).
"""

from typing import Literal

ModelSize = Literal["small", "large"]


def get_chat_model(size: ModelSize = "small"):
    """Return a LangChain chat model: `small` for routing and extraction, `large` for planning and writing."""
    raise NotImplementedError
