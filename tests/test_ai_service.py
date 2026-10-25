import pytest

from ai_service import analyze_repair_request


@pytest.mark.asyncio
async def test_ai_analysis_is_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert await analyze_repair_request("Компьютер показывает ошибку при запуске") is None
