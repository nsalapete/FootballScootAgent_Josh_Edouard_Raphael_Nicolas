"""Tests of the LLM client factory and of the scripted fake model (no network). Owner: D."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from app.config import get_settings
from app.llm.client import FakeChatModel, get_chat_model, json_output, structured, tool_call


class Extraction(BaseModel):
    """Small schema used to test structured output."""

    player: str
    confidence: float = 0.5


@tool
def search_players(position: str) -> str:
    """Search players by position."""
    return position


def use_provider(monkeypatch, provider: str, **extra: str) -> None:
    """Point the settings to `provider` (and any extra variables), bypassing the cache."""
    monkeypatch.setenv("LLM_PROVIDER", provider)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_fake_provider_is_the_default_in_tests():
    assert isinstance(get_chat_model("small"), FakeChatModel)
    assert isinstance(get_chat_model("large"), FakeChatModel)


def test_unknown_size_and_provider_are_rejected(monkeypatch):
    with pytest.raises(ValueError, match="size"):
        get_chat_model("medium")
    use_provider(monkeypatch, "ollama-magic")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        get_chat_model("small")


def test_anthropic_provider_builds_chat_anthropic(monkeypatch):
    use_provider(monkeypatch, "anthropic", ANTHROPIC_API_KEY="sk-test", LLM_MODEL_LARGE="claude-test-large")
    model = get_chat_model("large")
    assert type(model).__name__ == "ChatAnthropic"
    assert model.model == "claude-test-large"


def test_openai_compatible_provider_uses_base_url(monkeypatch):
    use_provider(
        monkeypatch,
        "openai_compatible",
        OPENAI_API_KEY="sk-test",
        LLM_BASE_URL="https://nano-gpt.example/api/v1",
        LLM_MODEL_SMALL="tiny-model",
    )
    model = get_chat_model("small")
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "tiny-model"
    assert str(model.openai_api_base).startswith("https://nano-gpt.example")


def test_fake_model_replays_text_then_default():
    model = FakeChatModel(script=["first answer"], default_reply="nothing left")
    assert model.invoke("hello").content == "first answer"
    assert model.invoke([HumanMessage("again")]).content == "nothing left"
    assert len(model.calls) == 2


def test_fake_model_returns_tool_calls_once_tools_are_bound():
    model = FakeChatModel(script=[tool_call("search_players", position="Defender")])
    bound = model.bind_tools([search_players])
    message = bound.invoke("find me a defender")
    assert isinstance(message, AIMessage)
    assert message.tool_calls[0]["name"] == "search_players"
    assert message.tool_calls[0]["args"] == {"position": "Defender"}
    assert model.bound_tools == ["search_players"]


def test_fake_model_tool_call_ids_are_unique():
    first, second = tool_call("a"), tool_call("a")
    assert first.tool_calls[0]["id"] != second.tool_calls[0]["id"]


def test_fake_model_accepts_callables():
    model = FakeChatModel(script=[lambda messages: f"seen {len(messages)} message(s)"])
    assert model.invoke("hi").content == "seen 1 message(s)"


def test_fake_structured_output_accepts_dict_json_and_objects():
    model = FakeChatModel(
        script=[
            {"player": "Diogo Santos", "confidence": 0.6},
            '{"player": "Joao Ferreira"}',
            Extraction(player="Gabriel Rocha", confidence=0.4),
        ],
        structured_defaults={"Extraction": {"player": "unknown"}},
    )
    runnable = model.with_structured_output(Extraction)
    assert runnable.invoke("a").player == "Diogo Santos"
    assert runnable.invoke("b").confidence == 0.5
    assert runnable.invoke("c").player == "Gabriel Rocha"
    assert runnable.invoke("d").player == "unknown"


def test_fake_structured_output_rejects_invalid_items():
    model = FakeChatModel()
    with pytest.raises(ValueError):
        model.with_structured_output(Extraction).invoke("no default for a required field")


def test_structured_uses_native_output_for_fake_models():
    model = FakeChatModel(script=[{"player": "Diogo Santos"}])
    assert structured(model, Extraction).invoke("x").player == "Diogo Santos"


def test_json_output_parses_fenced_json_and_retries_once():
    model = FakeChatModel(
        script=[
            "<think>let me think</think>Sure:\n```json\n{\"player\": \"Joao Ferreira\", \"confidence\": 0.7}\n```",
            "not json at all",
            '{"confidence": 0.1}',
            '{"player": "Gabriel Rocha"}',
        ]
    )
    first = json_output(model, Extraction, [("human", "extract")])
    assert (first.player, first.confidence) == ("Joao Ferreira", 0.7)
    with pytest.raises(ValueError):
        json_output(model, Extraction, [("human", "extract")])  # two invalid replies in a row
    assert json_output(model, Extraction, [("human", "extract")], retries=0).player == "Gabriel Rocha"
    assert "JSON schema" in model.calls[0][-1].content


def test_structured_falls_back_to_json_prompting_for_other_models(monkeypatch):
    use_provider(monkeypatch, "openai_compatible", OPENAI_API_KEY="sk-test", LLM_BASE_URL="https://x.example/v1")
    runnable = structured(get_chat_model("small"), Extraction)
    assert type(runnable).__name__ == "RunnableLambda"
