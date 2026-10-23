"""Ретрай временных ошибок агента OpenHands.

Провайдер LLM бывает временно недоступен (503/429/таймаут) — тогда агент
переходит в состояние error, но задачу хоронить нельзя: ждём, будим
разговор и продолжаем. Реальные сбои (кривой вызов инструмента и т.п.)
по-прежнему сразу помечаются failed.
"""

from orchestrator.core.scheduler import Scheduler


def test_transient_error_markers():
    transient = (
        "RuntimeError: There was an unexpected error while running the agent: "
        "ServiceUnavailableError. You can refresh the page or ask the agent "
        "to try again.",
        "Error code: 503 - {'message': 'Compatible model providers are "
        "temporarily unavailable'}",
        "Error code: 429 - RateLimitError: Too many requests",
        "litellm.exceptions.APIConnectionError: Connection error",
        "APITimeoutError: Request timed out",
    )
    for reason in transient:
        assert Scheduler._is_transient_agent_error(reason), reason


def test_fatal_error_markers():
    fatal = (
        "",
        None,
        "Missing required parameters for function 'execute_bash': "
        "{'security_risk'}",
        "Error code: 401 - Invalid API key",
        "Error code: 404 - model not found",
    )
    for reason in fatal:
        assert not Scheduler._is_transient_agent_error(reason), reason
