"""Регрессионные тесты стоимости задачи (issue #38: 0,87 ₽ вместо ~165 ₽).

Корень: эндпоинт OpenHands `GET /api/conversations/{id}/events` пагинирован
и без параметров отдаёт первые 20 событий (из НАЧАЛА разговора). get_usage
брал accumulated_token_usage из этого начала — стоимость получалась в сотни
раз заниженной. Правильно: запрашивать последние события (reverse=true) —
accumulated_token_usage в новейшем событии есть итог всего разговора.

Тесты не ходят в сеть: httpx.AsyncClient подменяется фейком.
"""

import json
from unittest.mock import MagicMock

from orchestrator.adapters.openhands import OpenHandsAdapter
from orchestrator.core import pricing


def _metrics_event(prompt: int, completion: int, cache_read: int, event_id: int) -> dict:
    return {
        "id": event_id,
        "llm_metrics": {
            "accumulated_token_usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cache_read_tokens": cache_read,
            }
        },
    }


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Фейк httpx.AsyncClient: помнит параметры запроса, отдаёт payload."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.params: dict | None = None

    async def get(self, url, params=None, headers=None):
        self.params = dict(params or {})
        return FakeResponse(self.payload)


def make_adapter(payload: dict) -> tuple[OpenHandsAdapter, FakeClient]:
    adapter = object.__new__(OpenHandsAdapter)
    adapter.base_url = "http://localhost:3000"
    adapter.api_key = None
    client = FakeClient(payload)
    adapter._client = client
    return adapter, client


def test_get_usage_uses_reverse_and_takes_newest_accumulated():
    """get_usage должен запрашивать ПОСЛЕДНИЕ события (reverse=true) и брать
    накопленные метрики из новейшего события, а не из начала разговора.

    Регресс issue #38: без reverse берётся accumulated из первых событий —
    109 578 prompt вместо 13 018 473, и стоимость падает до 0,87 ₽.
    """
    # /events?reverse=true возвращает события НОВЕЙШИМИ ПЕРВЫМИ.
    payload = {"events": [
        _metrics_event(13018473, 281071, 946611, event_id=900),  # новейшее
        _metrics_event(109578, 1570, 66816, event_id=10),        # из начала
    ]}
    adapter, client = make_adapter(payload)
    import asyncio

    usage = asyncio.run(adapter.get_usage("conv1"))

    assert client.params is not None, "параметры запроса не переданы"
    assert str(client.params.get("reverse")).lower() == "true", (
        "без reverse эндпоинт отдаёт только НАЧАЛО разговора"
    )
    assert usage == {
        "prompt_tokens": 13018473,
        "completion_tokens": 281071,
        "cache_read_tokens": 946611,
    }, "взяты накопления из начала разговора, а не из новейшего события"


def test_get_events_returns_chronological_order():
    """get_events разворачивает reverse-ответ в хронологический порядок —
    /status должен показывать последние команды, а не первые."""
    payload = {"events": [
        {"id": 30, "observation": "run"},
        {"id": 29, "observation": "run"},
        {"id": 28, "observation": "run"},
    ]}
    adapter, client = make_adapter(payload)
    import asyncio

    events = asyncio.run(adapter.get_events("conv1"))

    assert [e["id"] for e in events] == [28, 29, 30]


def test_compute_cost_matches_provider_billing():
    """Стоимость по реальным токенам задачи #38 и ценам glm-5.3-flash
    должна попасть в порядок биллинга provod.ai (~165 ₽), а не 0,87 ₽."""

    class FakeState:
        def get_setting(self, key, default=None):
            return "82.8" if key == "rub_per_usd" else default

        def get_price(self, model):
            return {
                "prompt_usd": 1.5e-07,
                "completion_usd": 5e-07,
                "cache_read_usd": 5e-08,
            }

    cost_rub, cost_usd = pricing.compute_cost(
        FakeState(), "glm-5.3-flash",
        prompt_tokens=13018473,
        completion_tokens=281071,
        cache_read_tokens=946611,
    )
    # 12 071 862 non-cache × 1.5e-7 + 946 611 × 5e-8 + 281 071 × 5e-7
    # ≈ 1.9986 USD ≈ 165.5 ₽
    assert 150 <= cost_rub <= 200, (
        f"стоимость {cost_rub} ₽ вне порядка биллинга provod.ai (150–200 ₽)"
    )
    assert abs(cost_usd - 1.9986) < 0.01
