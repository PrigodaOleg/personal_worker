"""Вечный loading разговора OpenHands + вотчер GHA сообщает о провале.

1. Холодный старт: разговор может навсегда зависнуть в loading (runtime
   готов, но агентский цикл не обработал начальное сообщение — реальный
   случай на задаче #45: 35 минут loading до ручной отмены). Монитор
   перезапускает разговор, затем пересоздаёт... точнее — падает с внятным
   текстом (пересоздание делает ретрай-логика задачи).
2. Вотчер GHA: при провале сборки по таймауту слежения задача получает
   запись в лог, а автор/админ — уведомление (раньше вотчер молча
   заканчивал: «ссылки не дописаны» только в task.log).
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeOpenHands:
    """get_agent_state отдаёт последовательность состояний."""

    def __init__(self, states):
        self._states = list(states)
        self.started = 0

    async def get_agent_state(self, conversation_id):
        if not self._states:
            return {"agent_state": "loading", "work_events": 0}
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    async def start_conversation(self, conversation_id):
        self.started += 1
        return {"status": "ok"}

    async def send_message(self, conversation_id, message):
        return {"success": True}


def make_monitor_scheduler(source: TaskSource):
    s = object.__new__(Scheduler)
    s.config = types.SimpleNamespace(
        clarification_poll_seconds=0.001,
        clarification_timeout_seconds=5,
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
        openhands_llm_model="m",
        imac_host=None,
        imac_user=None,
        imac_ssh_key="",
        secrets_dir="/nonexistent-secrets",
        model_fallbacks=lambda: [],  # тесты старых веток: fallback отключён
    )
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        get=lambda _id: None,
        get_setting=lambda k, d=None: d,
        set_setting=lambda k, v: None,
    )
    s.openhands = FakeOpenHands([])
    s._pending_agent_questions = {}
    s._pending_clarifications = {}
    s._pending_notifications = set()
    s._background_tasks = set()
    s._running_tasks = set()
    s._admin_answer_chat = {}
    s._repo_adapters = {}
    s._notify_author_calls: list = []
    s._notify_admin_calls: list = []

    def notify_author(task, text):
        s._notify_author_calls.append(text)

    def notify_admin(text):
        s._notify_admin_calls.append(text)

    s._notify_author = notify_author
    s._notify_admin = notify_admin

    async def noop(*a, **kw):
        return {}

    s._finalize_cost = noop
    s._build_ios_and_create_pr = noop
    s._ensure_build_secrets_in_runtime = noop
    s._ensure_imac_ssh_in_runtime = noop
    # Атрибуты планировщика.
    s.error_retry_wait_seconds = 0.001
    # Отрицательный дедлайн = условие «висит в loading» срабатывает сразу.
    s.loading_startup_timeout_seconds = -1
    s.max_stop_restarts = 3
    s.max_auto_continues = 4
    s.max_error_retries = 5
    s.max_state_misses = 3
    s.gha_watch_poll_seconds = 60
    s.gha_watch_timeout_seconds = 5400
    return s


def make_task(source: TaskSource) -> Task:
    return Task(
        id="tk2",
        source=source,
        source_id="45" if source == TaskSource.GITHUB else "1",
        title="e2e тесты",
        description="",
        status=TaskStatus.RUNNING,
        conversation_id="conv2",
    )


def monitor(s: Scheduler, task: Task) -> asyncio.Task:
    return asyncio.ensure_future(
        s._monitor_openhands(task, task.conversation_id, "/tmp/ws")
    )


def test_eternal_loading_restarts_then_fails_with_clear_text():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        # Вечный loading без событий: 2 рестарта не помогают.
        loading = {"agent_state": "loading", "work_events": 0}
        s.openhands = FakeOpenHands([loading])
        await monitor(s, task)
        assert s.openhands.started == 2  # два перезапуска разговора
        assert task.status == TaskStatus.FAILED
        assert any("не смог стартовать" in t for t in s._notify_author_calls)
        assert any("loading" in t for t in s._notify_admin_calls)

    asyncio.run(scenario())


def test_loading_with_work_events_is_not_cold_start():
    """loading с событиями в траектории — не холодный старт, не трогаем."""

    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        s.openhands = FakeOpenHands([
            {"agent_state": "loading", "work_events": 5},
        ])
        mon = monitor(s, task)
        await asyncio.sleep(0.05)
        assert not mon.done() or task.status != TaskStatus.FAILED
        assert s.openhands.started == 0
        mon.cancel()

    asyncio.run(scenario())


def test_normal_start_passes_without_restarts():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        s.loading_startup_timeout_seconds = 600  # нормальный дедлайн
        task = make_task(TaskSource.GITHUB)
        s.openhands = FakeOpenHands([
            {"agent_state": "loading", "work_events": 0},
            {"agent_state": "finished", "completed": True, "message": "done"},
        ])
        await monitor(s, task)
        assert s.openhands.started == 0  # loading прошёл сам, без вмешательства
        assert task.status == TaskStatus.DONE

    asyncio.run(scenario())
