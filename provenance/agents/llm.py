from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
from dotenv import load_dotenv

load_dotenv()


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = ROOT / "data" / "cache" / "llm"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str | None
    timeout_seconds: float
    optional: bool = False


@dataclass(frozen=True)
class LLMConfig:
    providers: tuple[ProviderConfig, ...]
    cache_dir: Path = DEFAULT_CACHE_DIR
    max_calls_per_question: int = 4
    retry_backoff_seconds: float = 0.5

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LLMConfig":
        values = os.environ if env is None else env
        providers = (
            ProviderConfig(
                "groq",
                values.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                values.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                "GROQ_API_KEY",
                float(values.get("GROQ_TIMEOUT_SECONDS", "20")),
            ),
            ProviderConfig(
                "gemini",
                values.get(
                    "GEMINI_BASE_URL",
                    "https://generativelanguage.googleapis.com/v1beta/openai/",
                ),
                values.get("GEMINI_MODEL", "gemini-3.8-flash"),
                "GEMINI_API_KEY",
                float(values.get("GEMINI_TIMEOUT_SECONDS", "25")),
            ),
            ProviderConfig(
                "ollama",
                values.get("OLLAMA_BASE_URL", "http://localhost:11434/v1/"),
                values.get("OLLAMA_MODEL", "qwen3:8b"),
                None,
                float(values.get("OLLAMA_TIMEOUT_SECONDS", "60")),
                optional=True,
            ),
        )
        return cls(
            providers=providers,
            cache_dir=Path(values.get("LLM_CACHE_DIR", str(DEFAULT_CACHE_DIR))),
            max_calls_per_question=int(values.get("LLM_MAX_CALLS_PER_QUESTION", "4")),
            retry_backoff_seconds=float(values.get("LLM_RETRY_BACKOFF_SECONDS", "0.5")),
        )


@dataclass
class LLMBudget:
    max_calls: int = 4
    calls_used: int = 0

    def consume(self) -> None:
        if self.calls_used >= self.max_calls:
            raise LLMBudgetExceeded(
                f"LLM call budget exhausted ({self.max_calls} calls per question)."
            )
        self.calls_used += 1


@dataclass
class LLMResponse:
    provider: str
    model: str
    data: dict[str, Any]
    cached: bool = False

    @property
    def message(self) -> dict[str, Any]:
        return self.data["choices"][0]["message"]


@dataclass
class LLMProviderError(RuntimeError):
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "All configured LLM providers failed: " + "; ".join(self.errors)


class LLMBudgetExceeded(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self.transport = transport
        self.sleep = sleep

    def _api_key(self, provider: ProviderConfig) -> str | None:
        if provider.api_key_env is None:
            return None
        return os.environ.get(provider.api_key_env)

    def _redact(self, text: str) -> str:
        redacted = text
        for provider in self.config.providers:
            key = self._api_key(provider)
            if key:
                redacted = redacted.replace(key, "[REDACTED]")
        redacted = re.sub(
            r"(?i)(?:gsk_[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{12,})",
            "[REDACTED]",
            redacted,
        )
        return redacted

    def _cache_payload(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
    ) -> bytes:
        value = {
            "provider": provider.name,
            "model": provider.model,
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    def _cache_path(self, payload: bytes) -> Path:
        digest = hashlib.sha256(payload).hexdigest()
        return self.config.cache_dir / f"{digest}.json"

    def _load_cache(self, path: Path, provider: ProviderConfig) -> LLMResponse | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return LLMResponse(provider=provider.name, model=provider.model, data=data, cached=True)

    def _write_cache(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".llm-",
            suffix=".tmp",
            delete=False,
        ) as cache_file:
            json.dump(data, cache_file, sort_keys=True, ensure_ascii=True)
            temp_path = Path(cache_file.name)
        os.replace(temp_path, path)

    def _client(self, provider: ProviderConfig) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(provider.timeout_seconds),
            transport=self.transport,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        budget: LLMBudget | None = None,
    ) -> LLMResponse:
        call_budget = budget or LLMBudget(self.config.max_calls_per_question)
        provider_cache: list[tuple[ProviderConfig, bytes, Path]] = []
        for provider in self.config.providers:
            cache_payload = self._cache_payload(provider, messages, tools, response_format)
            cache_path = self._cache_path(cache_payload)
            provider_cache.append((provider, cache_payload, cache_path))
            cached = self._load_cache(cache_path, provider)
            if cached is not None:
                return cached

        errors: list[str] = []
        for provider, _, cache_path in provider_cache:
            if provider.api_key_env and not self._api_key(provider):
                errors.append(f"{provider.name}: missing {provider.api_key_env}")
                continue
            headers = {"Content-Type": "application/json"}
            api_key = self._api_key(provider)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            elif provider.name == "ollama":
                headers["Authorization"] = "Bearer ollama"
            body: dict[str, Any] = {
                "model": provider.model,
                "messages": messages,
                "temperature": 0,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            if response_format:
                body["response_format"] = response_format
            url = provider.base_url.rstrip("/") + "/chat/completions"
            retryable_attempts = 0
            while True:
                call_budget.consume()
                try:
                    with self._client(provider) as client:
                        response = client.post(url, headers=headers, json=body)
                    if response.status_code == 429 or 500 <= response.status_code <= 599:
                        if retryable_attempts == 0:
                            retryable_attempts += 1
                            self.sleep(self.config.retry_backoff_seconds)
                            continue
                        errors.append(f"{provider.name}: HTTP {response.status_code} after retry")
                        break
                    if response.status_code >= 400:
                        errors.append(f"{provider.name}: HTTP {response.status_code}")
                        break
                    data = response.json()
                    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                        errors.append(f"{provider.name}: invalid chat-completion payload")
                        break
                    self._write_cache(cache_path, data)
                    return LLMResponse(provider.name, provider.model, data)
                except LLMBudgetExceeded:
                    raise
                except httpx.TimeoutException as error:
                    errors.append(f"{provider.name}: timeout ({self._redact(str(error))})")
                    break
                except (httpx.RequestError, json.JSONDecodeError, OSError, ValueError) as error:
                    errors.append(f"{provider.name}: {self._redact(str(error))}")
                    break
        raise LLMProviderError([self._redact(error) for error in errors])

    def llm_available(self) -> list[dict[str, Any]]:
        availability: list[dict[str, Any]] = []
        for provider in self.config.providers:
            if provider.api_key_env and not self._api_key(provider):
                availability.append(
                    {"provider": provider.name, "model": provider.model, "available": False, "reason": "missing_key"}
                )
                continue
            headers = {}
            api_key = self._api_key(provider)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            elif provider.name == "ollama":
                headers["Authorization"] = "Bearer ollama"
            try:
                url = provider.base_url.rstrip("/") + "/models"
                with self._client(provider) as client:
                    response = client.get(url, headers=headers)
                if response.status_code >= 400:
                    availability.append(
                        {"provider": provider.name, "model": provider.model, "available": False, "reason": f"HTTP {response.status_code}"}
                    )
                    continue
                data = response.json()
                model_ids = {
                    str(item.get("id"))
                    for item in data.get("data", [])
                    if isinstance(item, dict)
                } if isinstance(data, dict) else set()
                matching_model = provider.model in model_ids or not model_ids
                availability.append(
                    {
                        "provider": provider.name,
                        "model": provider.model,
                        "available": bool(matching_model),
                        "reason": "reachable" if matching_model else "model_unavailable",
                    }
                )
            except (httpx.RequestError, httpx.TimeoutException, json.JSONDecodeError, ValueError) as error:
                availability.append(
                    {"provider": provider.name, "model": provider.model, "available": False, "reason": self._redact(str(error))}
                )
        return availability


def llm_available(
    config: LLMConfig | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    return LLMClient(config, transport=transport).llm_available()
