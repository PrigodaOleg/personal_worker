"""Убийство задачи OpenHands (AgentStuckInLoopError): ретрай и уведомления.

OpenHands сам останавливает зациклившегося агента — error_reason содержит
AgentStuckInLoopError. Такая задача получает ОДИН ретрай (свежий старт
разговора), при повторе — падает с явным «убита OpenHands»:
- комментарий в issue (GH-задачи) / сообщение автору (TG-задачи);
- админу отдельное уведомление (для GH-задач);
- 🪓 в карточке/статусе/списке задач.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus

STUCK_REASON = "AgentStuckInLoopError: Agent got stuck in a loop"


class FakeOpenHands:
    """get_agent_state отдаёт последовательность состояний."""

    def __init__(self, states):
        self._states = list(states)
        self.started = 0
        self.sent: list[tuple[str, str]] = []

    async def get_agent_state(self, conversation_id):
        return self._states.pop(0) if self._states else self._states[-1]

    async def start_conversation(self, conversation_id):
        self.started += 1
        return {"status": "ok"}

    async def send_message(self, conversation_id, message):
        self.sent.append((conversation_id, message))
        return {"success": True}


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[int, str]] = []

    def create_comment(self, issue_number, body):
        self.comments.append((issue_number, body))
        return 1


def make_task(source: TaskSource) -> Task:
    return Task(
        id="tk1",
        source=source,
        source_id="43" if source == TaskSource.GITHUB else "100",
        title="firebase rules",
        description="",
        status=TaskStatus.RUNNING,
        conversation_id="conv1",
    )


def make_monitor_scheduler(source: TaskSource):
    """Scheduler с замоканными _monitor_openhands-зависимостями."""
    s = object.__new__(Scheduler)
    s.config = types.SimpleNamespace(
        clarification_poll_seconds=0.001,
        clarification_timeout_seconds=5,
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
        telegram_admin_username="boss",
        openhands_llm_model="m",
        imac_host=None,
        imac_user=None,
        imac_ssh_key="",
        secrets_dir="/nonexistent-secrets",
    )
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        get=lambda _id: None,
        get_setting=lambda k, d=None: d,
        set_setting=lambda k, v: None,
    )
    s.openhands = FakeOpenHands([])
    s.telegram = FakeTelegram()
    s.github = FakeGitHub()
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
        if task.source == TaskSource.GITHUB:
            s.github.comments.append((int(task.source_id), text))

    def notify_admin(text):
        s._notify_admin_calls.append(text)
        s.telegram.sent.append((999, text))

    s._notify_author = notify_author
    s._notify_admin = notify_admin
    # Атрибуты планировщика (обычно задаёт __init__, который в тесте не звался).
    s.error_retry_wait_seconds = 0.001
    s.max_stop_restarts = 3
    s.max_auto_continues = 4
    s.max_error_retries = 5
    s.max_state_misses = 3
    s.gha_watch_poll_seconds = 60
    s.gha_watch_timeout_seconds = 5400

    async def noop(*a, **kw):
        return {}

    s._finalize_cost = noop
    s._build_ios_and_create_pr = noop
    s._ensure_build_secrets_in_runtime = noop
    return s


def monitor(s: Scheduler, task: Task) -> asyncio.Task:
    return asyncio.ensure_future(
        s._monitor_openhands(task, task.conversation_id, "/tmp/ws")
    )


def test_stuck_error_gets_one_retry():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        s.openhands = FakeOpenHands([
            {"agent_state": "error", "message": "msg", "error_reason": STUCK_REASON},
            {"agent_state": "running", "message": "", "work_events": 5},
            {"agent_state": "finished", "message": "", "completed": True},
        ])
        await monitor(s, task)
        assert s.openhands.started == 1  # свежий старт разговора
        assert any("зацикливание" in c for _i, c in s.github.comments)
        assert s._notify_admin_calls == []  # финального убийства не было

    asyncio.run(scenario())


def test_stuck_error_twice_fails_with_explicit_kill():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        s.openhands = FakeOpenHands([
            {"agent_state": "error", "message": "msg", "error_reason": STUCK_REASON},
            {"agent_state": "error", "message": "msg", "error_reason": STUCK_REASON},
        ])
        await monitor(s, task)
        assert task.status == TaskStatus.FAILED
        # В issue — явное «убита OpenHands».
        assert any("убил задачу" in c for _i, c in s.github.comments)
        # Админ получил уведомление об убийстве задачи из GH.
        assert any("убита OpenHands" in t for t in s._notify_admin_calls)
        assert any("#43" in t for t in s._notify_admin_calls)
        # След в логе задачи — для карточки.
        assert s._killed_by_openhands(task)

    asyncio.run(scenario())


def test_stuck_error_tg_task_notifies_author_not_admin():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.TELEGRAM)
        task = make_task(TaskSource.TELEGRAM)
        s.openhands = FakeOpenHands([
            {"agent_state": "error", "message": "msg", "error_reason": STUCK_REASON},
            {"agent_state": "error", "message": "msg", "error_reason": STUCK_REASON},
        ])
        await monitor(s, task)
        assert task.status == TaskStatus.FAILED
        # Автору (в его чат) — явное уведомление.
        assert any("убил задачу" in t for t in s._notify_author_calls)
        # Админу про TG-задачу уведомление об убийстве не шлётся.
        assert s._notify_admin_calls == []

    asyncio.run(scenario())


def test_regular_error_not_marked_as_killed():
    async def scenario():
        s = make_monitor_scheduler(TaskSource.GITHUB)
        task = make_task(TaskSource.GITHUB)
        s.openhands = FakeOpenHands([
            {"agent_state": "error", "message": "boom", "error_reason": "SomethingElse"},
        ])
        await monitor(s, task)
        assert task.status == TaskStatus.FAILED
        assert not s._killed_by_openhands(task)
        assert not any("убил" in c for _i, c in s.github.comments)

    asyncio.run(scenario())


def test_is_stuck_loop_error_matcher():
    assert Scheduler._is_stuck_loop_error(STUCK_REASON)
    assert Scheduler._is_stuck_loop_error("Agent got stuck in a loop")
    assert not Scheduler._is_stuck_loop_error("ServiceUnavailable")
    assert not Scheduler._is_stuck_loop_error("")


def test_card_marks_killed_task():
    s = make_monitor_scheduler(TaskSource.GITHUB)
    task = make_task(TaskSource.GITHUB)
    task.status = TaskStatus.FAILED
    assert "🪓" not in s._task_card(task)
    task.add_log("OpenHands killed task: агент зациклился (AgentStuckInLoopError)")
    card = s._task_card(task)
    assert card.startswith("🪓")
    assert "Убита OpenHands" in card
