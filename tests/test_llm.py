from __future__ import annotations

import json

import httpx
import pytest

from provenance.agents.llm import (
    LLMBudget,
    LLMBudgetExceeded,
    LLMClient,
    LLMConfig,
    LLMProviderError,
    ProviderConfig,
)


def _config(tmp_path, *, max_calls: int = 4) -> LLMConfig:
    return LLMConfig(
        providers=(
            ProviderConfig("groq", "https://groq.test/v1", "g-model", "TEST_GROQ_KEY", 0.01),
            ProviderConfig("gemini", "https://gemini.test/v1", "g-model-2", "TEST_GEMINI_KEY", 0.01),
        ),
        cache_dir=tmp_path,
        max_calls_per_question=max_calls,
        retry_backoff_seconds=0.01,
    )


def _success(provider: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": provider}}]},
    )


def _set_keys(monkeypatch) -> None:
    monkeypatch.setenv("TEST_GROQ_KEY", "gsk_test_groq_key_123456789")
    monkeypatch.setenv("TEST_GEMINI_KEY", "gemini_test_key_123456789")


def test_success_uses_first_provider_and_caches_response(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _success("groq")

    client = LLMClient(_config(tmp_path), transport=httpx.MockTransport(handle))
    budget = LLMBudget(4)
    first = client.chat([{"role": "user", "content": "hello"}], budget=budget)
    second = client.chat([{"role": "user", "content": "hello"}], budget=LLMBudget(4))

    assert first.provider == "groq"
    assert first.message["content"] == "groq"
    assert not first.cached
    assert second.cached
    assert len(requests) == 1
    assert budget.calls_used == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert requests[0].headers["Authorization"] == "Bearer gsk_test_groq_key_123456789"


def test_429_retries_once_then_falls_back(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    calls = {"groq": 0, "gemini": 0}
    sleeps = []

    def handle(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls["groq" if host == "groq.test" else "gemini"] += 1
        if host == "groq.test":
            return httpx.Response(429, json={"error": "rate limited"})
        return _success("gemini")

    client = LLMClient(
        _config(tmp_path),
        transport=httpx.MockTransport(handle),
        sleep=sleeps.append,
    )
    budget = LLMBudget(4)
    response = client.chat([{"role": "user", "content": "question"}], budget=budget)

    assert response.provider == "gemini"
    assert calls == {"groq": 2, "gemini": 1}
    assert sleeps == [0.01]
    assert budget.calls_used == 3


def test_timeout_falls_back_without_retry(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "groq.test":
            raise httpx.ReadTimeout("timed out")
        return _success("gemini")

    client = LLMClient(_config(tmp_path), transport=httpx.MockTransport(handle))
    response = client.chat([{"role": "user", "content": "question"}], budget=LLMBudget(4))

    assert response.provider == "gemini"
    assert calls == ["groq.test", "gemini.test"]


def test_budget_exhaustion_counts_attempts(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    client = LLMClient(
        _config(tmp_path),
        transport=httpx.MockTransport(lambda request: _success("groq")),
    )
    budget = LLMBudget(1)
    client.chat([{"role": "user", "content": "first"}], budget=budget)

    with pytest.raises(LLMBudgetExceeded, match="budget exhausted"):
        client.chat([{"role": "user", "content": "second"}], budget=budget)
    assert budget.calls_used == 1


def test_provider_errors_redact_api_keys(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    secret = "gsk_test_groq_key_123456789"

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"upstream echoed {secret}", request=request)

    client = LLMClient(_config(tmp_path), transport=httpx.MockTransport(handle))
    with pytest.raises(LLMProviderError) as error:
        client.chat([{"role": "user", "content": "question"}], budget=LLMBudget(4))
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_availability_checks_models_endpoint_without_chat_call(tmp_path, monkeypatch) -> None:
    _set_keys(monkeypatch)
    methods = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"data": [{"id": "g-model"}, {"id": "g-model-2"}]})

    client = LLMClient(_config(tmp_path), transport=httpx.MockTransport(handle))
    availability = client.llm_available()

    assert all(item["available"] for item in availability)
    assert methods == ["GET", "GET"]
    assert "TEST_GROQ_KEY" not in json.dumps(availability)
