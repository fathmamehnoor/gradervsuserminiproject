"""OpenRouter usage logging: one [provider-usage] line per call, parseable by analysis/cost.py.

No network: the client's create() is stubbed with a canned OpenRouter-shaped response.
"""

import importlib.util
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletion

from agent_interp_envs.providers.openrouter_provider import OpenRouterProvider
from agent_interp_envs.tool_calling import EXECUTE_COMMAND_TOOL

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cost", REPO_ROOT / "analysis" / "cost.py")
cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost)


def _response(usage: dict | None) -> ChatCompletion:
    body = {
        "id": "gen-1", "object": "chat.completion", "created": 0, "model": "deepseek/deepseek-v4-pro-0813",
        "provider": "DeepSeek",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "reasoning": "Let me look around.",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "execute_command", "arguments": "{\"command\": \"ls\"}"}}]}}],
    }
    if usage is not None:
        body["usage"] = usage
    return ChatCompletion.model_validate(body)


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    return OpenRouterProvider(model="deepseek/deepseek-v4-pro-0813", messages=[{"role": "user", "content": "hi"}],
                              tools=[EXECUTE_COMMAND_TOOL], reasoning_effort="low",
                              provider_preferences={"only": ["deepseek"]})


def test_request_asks_openrouter_for_usage_accounting(provider):
    assert provider.kwargs["extra_body"]["usage"] == {"include": True}
    assert provider.kwargs["extra_body"]["reasoning"] == {"effort": "low"}
    assert provider.kwargs["extra_body"]["provider"] == {"only": ["deepseek"]}


def test_invoke_prints_a_usage_line_that_cost_py_parses(provider, monkeypatch, capsys):
    usage = {"prompt_tokens": 12000, "completion_tokens": 350, "total_tokens": 12350,
             "prompt_tokens_details": {"cached_tokens": 11000},
             "completion_tokens_details": {"reasoning_tokens": 200}, "cost": 0.00135}
    monkeypatch.setattr(provider.client.chat.completions, "create", lambda **kw: _response(usage))
    result = provider.invoke()
    assert result.tool_calls[0].name == "execute_command"

    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("[provider-usage]"))
    assert "model=deepseek/deepseek-v4-pro-0813" in line and "served_by=DeepSeek" in line
    calls = cost.parse_usage(out)
    assert calls == [{"in": 12000, "cache_read": 11000, "out": 350, "reasoning": 200,
                      "cost": 0.00135, "served_by": "DeepSeek"}]


def test_missing_cost_and_details_still_log(provider, monkeypatch, capsys):
    usage = {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520}
    monkeypatch.setattr(provider.client.chat.completions, "create", lambda **kw: _response(usage))
    provider.invoke()
    calls = cost.parse_usage(capsys.readouterr().out)
    assert calls == [{"in": 500, "cache_read": 0, "out": 20, "reasoning": 0, "cost": None, "served_by": "DeepSeek"}]


def test_no_usage_no_line(provider, monkeypatch, capsys):
    monkeypatch.setattr(provider.client.chat.completions, "create", lambda **kw: _response(None))
    provider.invoke()
    assert "[provider-usage]" not in capsys.readouterr().out
