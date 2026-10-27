"""Сломанный tool-calling у LLM-провайдера + счётчик авто-продолжений.

Разбор задачи #46 (attempt 3): провайдер часами отдавал вызовы инструментов
обычным текстом (`<function=execute_bash>...`), они не исполнялись, агент
«работал» 4 часа без единой команды, а монитор бесконечно подталкивал его —
счётчик авто-продолжений сбрасывался каждый цикл, потому что прогресс
считался по work_events (OpenHands обнуляет их при каждом запуске агента).

Теперь:
- прогресс для сброса счётчика = рост исполненных команд (get_events);
- серия пауз с сырым вызовом инструмента БЕЗ ни одной исполненной команды —
  фейл-фаст с внятным диагнозом (провайдеру подталкивания не помогают).
"""

from __future__ import annotations

import asyncio

from tests.test_eternal_loading import (
    FakeOpenHands,
    make_monitor_scheduler,
    make_task,
    monitor,
)
from orchestrator.core.task import TaskSource, TaskStatus

RAW_CALL = (
    "<function=execute_bash><parameter=command>ls /workspace</parameter>"
    "</function>"
)


class FakeOpenHandsWithEvents(FakeOpenHands):
    """Состояния агента + события разговора (исполненные команды).

    События «растут» после каждого подталкивания (revealed) — как в жизни:
    агент после нуджа исполняет команды.
    """

    def __init__(self, states, events):
        super().__init__(states)
        self._events = events
        self.revealed = 0

    async def get_events(self, conversation_id, limit=100):
        return self._events[: self.revealed]

    async def send_message(self, conversation_id, message):
        self.revealed = min(self.revealed + 2, len(self._events))
        return {"success": True}


def test_raw_tool_calls_fail_fast():
    """3 паузы подряд с сырым вызовом и 0 команд — задача падает сразу."""

    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        pause = {
            "agent_state": "awaiting_user_input",
            "message": RAW_CALL,
            "work_events": 1,
        }
        s.openhands = FakeOpenHandsWithEvents([pause], events=[])
        await monitor(s, task)
        assert task.status == TaskStatus.FAILED
        assert any("tool-calling" in t for t in s._notify_author_calls)
        assert any("tool-calling" in t for t in s._notify_admin_calls)
        assert any("сломанный tool-calling" in e for e in task.log)

    asyncio.run(scenario())


def test_raw_tool_call_with_real_work_is_not_fatal():
    """Одиночный сырой вызов при работающем агенте — просто подталкиваем."""

    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        # Пауза с сырым вызовом, но команды исполнялись — провайдер в целом
        # работает; монитор подталкивает и ждёт, не роняя задачу.
        pause = {
            "agent_state": "awaiting_user_input",
            "message": f"Продолжаю. {RAW_CALL}",
            "work_events": 5,
        }
        s.openhands = FakeOpenHandsWithEvents(
            [pause], events=[{"action": "run", "message": "Running command: ls"}]
        )
        mon = monitor(s, task)
        await asyncio.sleep(0.05)
        mon.cancel()
        assert task.status == TaskStatus.RUNNING

    asyncio.run(scenario())


def test_auto_continue_counter_resets_only_on_real_commands():
    """Сброс счётчика — по росту исполненных команд, не по work_events."""

    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        # Пауза 1: объявление без команд → (1/4).
        # Пауза 2: агент исполнил 2 команды (прогресс) → сброс → (1/4).
        # Пауза 3: команд не прибавилось → счётчик растёт → (2/4).
        states = [
            {"agent_state": "awaiting_user_input",
             "message": "Начну с изучения структуры", "work_events": 1},
            {"agent_state": "running"},
            {"agent_state": "awaiting_user_input",
             "message": "Изучаю тесты", "work_events": 2},
            {"agent_state": "running"},
            {"agent_state": "awaiting_user_input",
             "message": "Смотрю pubspec", "work_events": 3},
        ]
        events = [
            {"action": "run", "message": "Running command: ls"},
            {"action": "run", "message": "Running command: cat pubspec.yaml"},
        ]
        s.openhands = FakeOpenHandsWithEvents(states, events=events)
        mon = monitor(s, task)
        await asyncio.sleep(0.05)
        mon.cancel()
        counters = [
            e.split("авто-продолжение ")[1]
            for e in task.log
            if "авто-продолжение" in e
        ]
        # Пауза 1 → (1/4); пауза 2 (были команды) → сброс → (1/4);
        # пауза 3 (команд не прибавилось) → (2/4).
        assert counters == ["(1/4)", "(1/4)", "(2/4)"], counters

    asyncio.run(scenario())
