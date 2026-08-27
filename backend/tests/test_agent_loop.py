"""The agent loop's mechanics, driven by a scripted model.

The model's judgement is not ours to test, but the loop around it is: tool
dispatch, the tool_result round-trip, multi-turn continuation, refusal handling
and the turn cap. A scripted client makes those deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from dataq.services.agent import AnalysisAgent

from .fixtures import write_auth_csv


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUse:
    name: str
    input: dict[str, Any]
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str = "end_turn"


class ScriptedClient:
    """Returns queued responses and records what it was sent."""

    def __init__(self, script: list[FakeResponse]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            return FakeResponse(content=[FakeTextBlock("done")], stop_reason="end_turn")
        return self.script.pop(0)


@pytest.fixture
def agent(app_ctx, run_op, tmp_path):
    run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=200)), name="auth")
    app_ctx.settings.anthropic_api_key = "test-key"
    return AnalysisAgent(app_ctx)


def script(agent: AnalysisAgent, responses: list[FakeResponse]) -> ScriptedClient:
    client = ScriptedClient(responses)
    agent._client = lambda: client  # noqa: SLF001 - deliberate injection
    return client


def test_single_turn_text_only(agent):
    script(agent, [FakeResponse(content=[FakeTextBlock("Hello.")], stop_reason="end_turn")])
    turns = list(agent.run("hi"))
    assert [t.type for t in turns] == ["text", "done"]
    assert turns[0].text == "Hello."


def test_tool_call_is_dispatched_and_result_fed_back(agent):
    client = script(agent, [
        FakeResponse(content=[FakeToolUse("list_datasets", {})], stop_reason="tool_use"),
        FakeResponse(content=[FakeTextBlock("You have one dataset.")], stop_reason="end_turn"),
    ])
    turns = list(agent.run("what do I have?"))
    types = [t.type for t in turns]
    assert types == ["tool_use", "tool_result", "text", "done"]

    # The tool actually ran against the real service layer.
    result = turns[1].tool_result
    assert isinstance(result, list) and result[0]["name"] == "auth"

    # The second request carries the assistant turn plus a tool_result user turn.
    second = client.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user"
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0]["tool_use_id"] == "tu_1"


def test_parallel_tool_calls_return_in_one_user_message(agent):
    client = script(agent, [
        FakeResponse(
            content=[
                FakeToolUse("list_datasets", {}, id="a"),
                FakeToolUse("list_plugins", {}, id="b"),
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[FakeTextBlock("ok")], stop_reason="end_turn"),
    ])
    list(agent.run("look at everything"))
    results = client.requests[1]["messages"][-1]["content"]
    # Splitting these across messages would train the model out of parallel calls.
    assert len(results) == 2
    assert {r["tool_use_id"] for r in results} == {"a", "b"}


def test_tool_error_is_marked_and_does_not_stop_the_loop(agent):
    script(agent, [
        FakeResponse(
            content=[FakeToolUse("run_query", {"query": {"dataset": "nope"}})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[FakeTextBlock("That dataset does not exist.")],
                     stop_reason="end_turn"),
    ])
    turns = list(agent.run("query a missing dataset"))
    result = next(t for t in turns if t.type == "tool_result").tool_result
    assert "error" in result
    assert turns[-1].type == "done"


def test_refusal_is_surfaced_not_crashed(agent):
    script(agent, [FakeResponse(content=[], stop_reason="refusal")])
    turns = list(agent.run("something disallowed"))
    assert turns[0].type == "error"
    assert "declined" in turns[0].text


def test_turn_cap_stops_a_runaway_loop(app_ctx, run_op, tmp_path):
    run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=50)), name="auth")
    app_ctx.settings.anthropic_api_key = "test-key"
    agent = AnalysisAgent(app_ctx, max_turns=3)
    # A model that never stops calling tools.
    script(agent, [
        FakeResponse(content=[FakeToolUse("list_datasets", {})], stop_reason="tool_use")
        for _ in range(10)
    ])
    turns = list(agent.run("loop forever"))
    assert turns[-1].type == "error"
    assert "stopped after 3 turns" in turns[-1].text
    assert sum(1 for t in turns if t.type == "tool_use") == 3


def test_system_prompt_is_cached_and_thinking_enabled(agent):
    client = script(agent, [FakeResponse(content=[FakeTextBlock("hi")])])
    list(agent.run("hello"))
    req = client.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["thinking"] == {"type": "adaptive"}
    assert req["model"] == "claude-opus-5"
    assert len(req["tools"]) >= 10
