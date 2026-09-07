"""Regression tests for the OpenAI request body the agent's LLM wiring emits.

Guards the gpt-luna gateway fix: that gateway injects a reasoning_effort
default, and /v1/chat/completions then rejects the call as soon as function
tools are attached. Sending reasoning_effort="none" explicitly overrides it.
"""
import json
import os
import unittest
from typing import Any
from unittest import mock

import httpx

# config.py reads these at import time, and agent.py imports config.
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("PB_URL", "http://localhost")
os.environ.setdefault("PB_EMAIL", "test@example.com")
os.environ.setdefault("PB_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

import agent

CANNED_COMPLETION: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@tool
def probe() -> str:
    """Probe tool, attached only to force a function-tools request."""
    return "ok"


def mock_openai_client(sink: dict[str, Any]) -> httpx.Client:
    """An httpx client that records the request body into sink and canned-replies."""

    def handler(request: httpx.Request) -> httpx.Response:
        sink.clear()
        sink.update(json.loads(request.content))
        return httpx.Response(200, json=CANNED_COMPLETION)

    return httpx.Client(transport=httpx.MockTransport(handler))


def capture_request_body(model: str) -> dict[str, Any]:
    """Return the /v1/chat/completions body sent for model with tools bound."""
    body: dict[str, Any] = {}
    llm = init_chat_model(
        model,
        model_provider="openai",
        http_client=mock_openai_client(body),
        **agent.openai_model_overrides(model),
    )
    llm.bind_tools([probe]).invoke("ping")
    return body


class OpenAIModelOverridesTest(unittest.TestCase):
    def test_luna_models_get_reasoning_effort_none(self) -> None:
        for model in ("gpt-5.6-luna", "gpt-luna", "GPT-5.6-Luna", "gpt-luna-mini"):
            with self.subTest(model=model):
                self.assertEqual(
                    agent.openai_model_overrides(model),
                    {"temperature": 1, "model_kwargs": {"reasoning_effort": "none"}},
                )

    def test_other_models_get_no_overrides(self) -> None:
        for model in ("gpt-4o-mini", "gpt-4o", "o1-mini", "llama3.1"):
            with self.subTest(model=model):
                self.assertEqual(agent.openai_model_overrides(model), {})

    def test_overrides_are_not_shared_between_calls(self) -> None:
        first = agent.openai_model_overrides("gpt-5.6-luna")
        first["model_kwargs"]["reasoning_effort"] = "high"
        self.assertEqual(
            agent.openai_model_overrides("gpt-5.6-luna")["model_kwargs"],
            {"reasoning_effort": "none"},
        )


class RequestBodyTest(unittest.TestCase):
    def test_luna_body_carries_reasoning_effort_none_with_tools(self) -> None:
        body = capture_request_body("gpt-5.6-luna")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["temperature"], 1)
        self.assertTrue(body["tools"], "function tools must still be attached")
        self.assertEqual(body["tools"][0]["type"], "function")

    def test_baseline_model_body_is_unchanged(self) -> None:
        body = capture_request_body("gpt-4o-mini")
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["model"], "gpt-4o-mini")


class BuildAgentWiringTest(unittest.TestCase):
    """build_agent must actually feed the overrides into init_chat_model."""

    def _init_kwargs(self, model: str, provider: str) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        body: dict[str, Any] = {}

        def fake_init(name: str, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return init_chat_model(
                name,
                model_provider="openai",
                http_client=mock_openai_client(body),
                **{k: v for k, v in kwargs.items() if k != "model_provider"},
            )

        with mock.patch.object(agent, "LLM_MODEL", model), \
             mock.patch.object(agent, "LLM_PROVIDER", provider), \
             mock.patch.object(agent, "init_chat_model", fake_init):
            agent.build_agent(lambda _qid: None, lambda _pid: None)
        return seen

    def test_openai_luna_model_receives_overrides(self) -> None:
        seen = self._init_kwargs("gpt-5.6-luna", "openai")
        self.assertEqual(seen["model_kwargs"], {"reasoning_effort": "none"})
        self.assertEqual(seen["temperature"], 1)

    def test_openai_baseline_model_receives_no_overrides(self) -> None:
        seen = self._init_kwargs("gpt-4o-mini", "openai")
        self.assertNotIn("model_kwargs", seen)
        self.assertNotIn("temperature", seen)

    def test_non_openai_provider_is_untouched(self) -> None:
        seen = self._init_kwargs("luna-local", "ollama")
        self.assertNotIn("model_kwargs", seen)
        self.assertNotIn("temperature", seen)


if __name__ == "__main__":
    unittest.main()
