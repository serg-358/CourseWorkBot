import json
import sys
from types import SimpleNamespace

import pytest

from ai_service import analyze_repair_request, normalize_catalog_item_codes


@pytest.mark.asyncio
async def test_ai_analysis_is_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("KODIKROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await analyze_repair_request("Компьютер показывает ошибку при запуске") is None


@pytest.mark.asyncio
async def test_ai_analysis_uses_kodikrouter_chat_completions(monkeypatch):
    captured: dict[str, object] = {}

    class NetworkError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            payload = {
                "priority": "medium",
                "client_causes": "Cause",
                "client_recommendations": "Recommendation",
                "safety_note": "Safety note",
                "master_diagnosis": "Diagnosis",
                "parts": "None",
                "tools": "Screwdriver",
                "catalog_item_codes": [],
            }
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            APIConnectionError=NetworkError,
            APITimeoutError=NetworkError,
            BadRequestError=BadRequestError,
            AsyncOpenAI=FakeClient,
        ),
    )
    monkeypatch.setenv("KODIKROUTER_API_KEY", "test-key")
    monkeypatch.setenv("KODIKROUTER_BASE_URL", "https://api.kodikrouter.ru/v1")
    monkeypatch.setenv("KODIKROUTER_MODEL", "openai/gpt-5.4-mini")

    result = await analyze_repair_request("Computer does not turn on")

    assert result is not None
    assert captured["client"] == {
        "api_key": "test-key",
        "base_url": "https://api.kodikrouter.ru/v1",
        "max_retries": 0,
    }
    assert captured["model"] == "openai/gpt-5.4-mini"
    assert captured["temperature"] == 0
    assert "response_format" in captured
    assert "всегда включай код diagnostics" in captured["messages"][0]["content"][0]["text"]


def test_normalize_catalog_item_codes_keeps_diagnostics_and_discards_unknown_codes():
    assert normalize_catalog_item_codes(["ssd_500gb", "missing", "ssd_500gb"]) == [
        "diagnostics",
        "ssd_500gb",
    ]


@pytest.mark.asyncio
async def test_ai_analysis_retries_without_temperature_when_gateway_rejects_it(monkeypatch):
    calls: list[dict[str, object]] = []

    class NetworkError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if "temperature" in kwargs:
                raise BadRequestError("temperature is not supported")
            payload = {
                "priority": "medium",
                "client_causes": "Cause",
                "client_recommendations": "Recommendation",
                "safety_note": "Safety note",
                "master_diagnosis": "Diagnosis",
                "parts": "None",
                "tools": "Screwdriver",
                "catalog_item_codes": [],
            }
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    class FakeClient:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=FakeCompletions())

        async def close(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(
            APIConnectionError=NetworkError,
            APITimeoutError=NetworkError,
            BadRequestError=BadRequestError,
            AsyncOpenAI=FakeClient,
        ),
    )
    monkeypatch.setenv("KODIKROUTER_API_KEY", "test-key")

    result = await analyze_repair_request("Компьютер не включается")

    assert result is not None
    assert result.catalog_item_codes == ["diagnostics"]
    assert len(calls) == 2
    assert calls[0]["temperature"] == 0
    assert "temperature" not in calls[1]
