"""Single entry point to the chat models. Owner: D.

`LLM_PROVIDER` selects the backend: `anthropic` (Claude API), `openai_compatible` (NanoGPT or any
OpenAI-compatible API, using `LLM_BASE_URL`) or `fake` (deterministic, no network: tests and CI).
"""

import json
import re
from functools import lru_cache
from itertools import count
from typing import Any, Callable, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, convert_to_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings

ModelSize = Literal["small", "large"]
PROVIDERS = ("anthropic", "openai_compatible", "fake")

_call_ids = count(1)


def tool_call(name: str, **args: Any) -> AIMessage:
    """Build an assistant message asking for one tool call (handy to script the fake model)."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"fake-call-{next(_call_ids)}"}])


def _to_message(item: Any, default_reply: str) -> AIMessage:
    """Turn one scripted item into an assistant message."""
    if item is None:
        return AIMessage(content=default_reply)
    if isinstance(item, AIMessage):
        return item
    if isinstance(item, BaseModel):
        return AIMessage(content=item.model_dump_json())
    if isinstance(item, (dict, list)):
        return AIMessage(content=json.dumps(item, ensure_ascii=False))
    return AIMessage(content=str(item))


class FakeChatModel(BaseChatModel):  # pylint: disable=abstract-method
    """Deterministic chat model replaying a script, so that tests never touch the network.

    Each call consumes the next item of `script`: an `AIMessage` (use `tool_call()` for tool calls),
    a string, a Pydantic object or a dict (for structured output), or a callable receiving the messages.
    Once the script is exhausted, plain calls answer `default_reply` and structured calls validate
    `structured_defaults[schema name]` (an empty dict by default).
    """

    script: list[Any] = Field(default_factory=list)
    default_reply: str = "I cannot answer from the graph."
    structured_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        """Identifier used by LangChain callbacks."""
        return "fake-scripted"

    def _next(self, messages: list[BaseMessage]) -> Any:
        """Record the call and pop the next scripted item (None when exhausted)."""
        self.calls.append(list(messages))
        if not self.script:
            return None
        item = self.script.pop(0)
        if callable(item) and not isinstance(item, (BaseMessage, BaseModel, type)):
            return item(messages)
        return item

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        """Return the next scripted message."""
        message = _to_message(self._next(messages), self.default_reply)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "FakeChatModel":
        """Remember the tool names; the script decides which tool calls come back."""
        self.bound_tools = [convert_to_openai_tool(tool)["function"]["name"] for tool in tools]
        return self

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any) -> RunnableLambda:
        """Return a runnable that validates the next scripted item against `schema`."""

        def run(value: Any) -> BaseModel:
            item = self._next(self._convert_input(value).to_messages())
            if isinstance(item, schema):
                return item
            if isinstance(item, BaseModel):
                return schema.model_validate(item.model_dump())
            if isinstance(item, dict):
                return schema.model_validate(item)
            if isinstance(item, str):
                return schema.model_validate_json(item)
            return schema.model_validate(self.structured_defaults.get(schema.__name__, {}))

        return RunnableLambda(run)


# (timeout in seconds, retries) per size: a stuck small model gives up quickly so the caller can fall back.
LIMITS = {"small": (30, 1), "large": (60, 2)}


def _build_anthropic(model: str, size: str, api_key: str, _base_url: str) -> BaseChatModel:
    """Claude through the Anthropic API (temperature 0: reproducible answers and evaluations)."""
    from langchain_anthropic import ChatAnthropic  # pylint: disable=import-outside-toplevel

    return ChatAnthropic(
        model=model,
        api_key=api_key or None,
        temperature=0,
        max_tokens=2048,
        timeout=LIMITS[size][0],
        max_retries=LIMITS[size][1],
    )


def _build_openai_compatible(model: str, size: str, api_key: str, base_url: str) -> BaseChatModel:
    """Any OpenAI-compatible endpoint (NanoGPT, a local server...) reached through `LLM_BASE_URL`."""
    from langchain_openai import ChatOpenAI  # pylint: disable=import-outside-toplevel

    return ChatOpenAI(
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
        temperature=0,
        timeout=LIMITS[size][0],
        max_retries=LIMITS[size][1],
    )


_BUILDERS: dict[str, Callable[[str, str, str, str], BaseChatModel]] = {
    "anthropic": _build_anthropic,
    "openai_compatible": _build_openai_compatible,
}


@lru_cache(maxsize=8)
def _cached_model(provider: str, model: str, size: str, api_key: str, base_url: str) -> BaseChatModel:
    """One client per configuration, so that every request reuses its HTTP connection pool."""
    return _BUILDERS[provider](model, size, api_key, base_url)


def structured(model: BaseChatModel, schema: type[BaseModel]):
    """`model` constrained to return a validated `schema` object.

    Claude and the fake model use LangChain's native structured output. OpenAI-compatible models get the JSON
    schema in the prompt instead: NanoGPT models refuse both the `json_schema` response format
    (`model_response_format_unsupported`) and forced tool calls (`tool_choice` other than `auto`).
    """
    if isinstance(model, FakeChatModel) or type(model).__name__ == "ChatAnthropic":
        return model.with_structured_output(schema)
    return RunnableLambda(lambda value: json_output(model, schema, value))


def parse_json_object(text: str) -> Any:
    """The JSON object of a reply, ignoring `<think>` blocks, code fences and prose around it."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start : end + 1])


def json_output(model: BaseChatModel, schema: type[BaseModel], value: Any, retries: int = 1) -> BaseModel:
    """Ask for one JSON object matching `schema`, validate it, and retry once with the error if it is invalid."""
    instruction = (
        "Reply with ONE JSON object only, no prose and no code fence, valid against this JSON schema: "
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )
    messages: list[BaseMessage] = [*convert_to_messages(value), SystemMessage(instruction)]
    for attempt in range(retries + 1):
        reply = model.invoke(messages)
        content = reply.content if isinstance(reply.content, str) else str(reply.content)
        try:
            return schema.model_validate(parse_json_object(content))
        except (ValueError, ValidationError) as error:
            if attempt == retries:
                raise
            messages += [reply, HumanMessage(f"Invalid answer ({str(error)[:300]}). Reply with the JSON object only.")]
    raise RuntimeError("unreachable")


def get_chat_model(size: ModelSize = "small"):
    """Return a LangChain chat model: `small` for routing and extraction, `large` for planning and writing."""
    if size not in ("small", "large"):
        raise ValueError(f"Unknown model size {size!r}: expected 'small' or 'large'")
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown LLM_PROVIDER {provider!r}: expected one of {', '.join(PROVIDERS)}")
    if provider == "fake":
        return FakeChatModel()  # never cached: a fake model holds its own script
    model = settings.llm_model_small if size == "small" else settings.llm_model_large
    api_key = settings.anthropic_api_key if provider == "anthropic" else settings.openai_api_key
    return _cached_model(provider, model, size, api_key, settings.llm_base_url)
