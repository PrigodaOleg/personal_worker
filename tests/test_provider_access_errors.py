"""Ошибки доступа к LLM-провайдеру: ноль на балансе / отклонённый ключ.

Провайдер отклоняет запросы (402 insufficient balance, 401 invalid key),
LiteLLM заворачивает их в непрозрачные исключения (402 приходит как
APIConnectionError). Ретраи бессмысленны — каждый запрос будет отклонён,
пока баланс/ключ не восстановят. Задача должна падать сразу с внятным
диагнозом, автору и админу уходят уведомления (это влияет на все задачи).
"""

from __future__ import annotations

import asyncio
import inspect

from tests.test_eternal_loading import (
    FakeOpenHands,
    make_monitor_scheduler,
    make_task,
    monitor,
)
from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import TaskSource, TaskStatus


def test_provider_access_error_classification():
    assert Scheduler._provider_access_error(
        "APIConnectionError: Error code: 402 - insufficient balance"
    ) == "ноль на балансе провайдера"
    assert Scheduler._provider_access_error(
        "litellm.AuthenticationError: invalid api key"
    ) == "ключ провайдера отклонён (401/403)"
    assert Scheduler._provider_access_error("Payment Required") == (
        "ноль на балансе провайдера"
    )
    assert Scheduler._provider_access_error(
        "Недостаточно средств на счёте"
    ) == "ноль на балансе провайдера"
    # Временные ошибки и пустота — НЕ про доступ.
    assert Scheduler._provider_access_error("rate limit exceeded") is None
    assert Scheduler._provider_access_error("AgentStuckInLoopError") is None
    assert Scheduler._provider_access_error("") is None


def test_billing_error_fails_fast_without_retries():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.TELEGRAM)
        task = make_task(TaskSource.TELEGRAM)
        # 402 завёрнут в APIConnectionError — раньше ушёл бы в transient-ретраи.
        error_state = {
            "agent_state": "error",
            "error_reason": (
                "APIConnectionError: Error code: 402 - "
                "{'error': {'message': 'insufficient balance'}}"
            ),
            "work_events": 0,
        }
        s.openhands = FakeOpenHands([error_state])
        await asyncio.wait_for(monitor(s, task), timeout=10)

        assert task.status == TaskStatus.FAILED
        assert s.openhands.started == 0, "ретраи при нулевом балансе бессмысленны"
        assert any("баланс" in t for t in s._notify_author_calls)
        assert s._notify_admin_calls, "админ должен узнать о нуле на балансе сразу"
        assert any("баланс" in t for t in task.log)

    asyncio.run(scenario())


def test_auth_error_fails_fast_without_retries():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        error_state = {
            "agent_state": "error",
            "error_reason": "litellm.AuthenticationError: Invalid API key provided",
            "work_events": 0,
        }
        s.openhands = FakeOpenHands([error_state])
        await asyncio.wait_for(monitor(s, task), timeout=10)

        assert task.status == TaskStatus.FAILED
        assert s.openhands.started == 0
        assert any("ключ" in t for t in s._notify_author_calls)
        assert s._notify_admin_calls

    asyncio.run(scenario())


def test_transient_errors_still_retried():
    """Rate limit — по-прежнему временная ошибка: ретраи сохраняются."""

    async def scenario():
        s = make_monitor_scheduler(TaskSource.TELEGRAM)
        task = make_task(TaskSource.TELEGRAM)
        # 429, потом вечный loading — задача не должна упасть по access-ветке.
        rate_limit = {
            "agent_state": "error",
            "error_reason": "RateLimitError: 429 too many requests",
            "work_events": 0,
        }
        s.openhands = FakeOpenHands([rate_limit])
        try:
            await asyncio.wait_for(monitor(s, task), timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass  # важно лишь то, что это НЕ провал по access-ветке
        assert "баланс" not in " ".join(s._notify_author_calls)

    asyncio.run(scenario())


def test_monitor_error_branch_has_access_check_before_retries():
    """Access-проверка стоит ДО transient-ретраев (402 приходит как
    APIConnectionError — transient-маркеры его перехватили бы)."""
    src = inspect.getsource(Scheduler._monitor_openhands)
    access_pos = src.find("_provider_access_error")
    transient_pos = src.find("_is_transient_agent_error")
    assert 0 < access_pos < transient_pos
