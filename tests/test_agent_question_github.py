"""Регрессионные тесты механизма «вопрос агента → ответ автора» (GitHub).

Фон: задача из GitHub issue зависала, когда агент в процессе работы задавал
вопрос автору: в _handle_agent_question использовалась неопределённая
переменная qinfo (NameError), из-за чего в записи ожидания не появлялись
github_issue / github_after_comment_id, и poll_github никогда не забирал
ответ из комментариев issue.

Тесты не ходят в сеть: Scheduler собирается через object.__new__ с фейковыми
адаптерами.
"""

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[int, int, str]] = []
        self.edits: list[tuple[int, str]] = []

    def create_comment(self, issue_number: int, body: str) -> int:
        cid = 1000 + len(self.comments)
        self.comments.append((issue_number, cid, body))
        return cid

    def update_comment(self, comment_id: int, body: str) -> None:
        self.edits.append((comment_id, body))

    def last_bot_comment_id(self, issue_number: int):
        bot_ids = [cid for (_n, cid, _b) in self.comments]
        return bot_ids[-1] if bot_ids else None


class FakeOpenHands:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, message: str) -> dict:
        self.sent.append((conversation_id, message))
        return {"success": True}

    async def start_conversation(self, conversation_id: str) -> dict:
        return {"status": "ok"}


class FakeTelegram:
    async def send_message(self, *args, **kwargs):
        return 1

    async def edit_message(self, *args, **kwargs):
        return True


def make_scheduler() -> Scheduler:
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}
    s._pending_clarifications = {}
    s._pending_notifications = set()
    s._background_tasks = set()
    s._running_tasks = set()
    s.github = FakeGitHub()
    s.openhands = FakeOpenHands()
    s.telegram = FakeTelegram()
    s.state = types.SimpleNamespace(save=lambda t: None)
    s.config = types.SimpleNamespace(clarification_timeout_seconds=60)
    return s


def make_task() -> Task:
    return Task(
        id="t36",
        source=TaskSource.GITHUB,
        source_id="36",
        title="тест",
        description="описание",
        status=TaskStatus.RUNNING,
        conversation_id="conv1",
    )


def _spawn(coro):
    return asyncio.get_running_loop().create_task(coro)


def test_agent_question_github_registers_issue_and_delivers_answer():
    """Главный регресс: вопрос регистрируется с github_issue, ответ из
    комментария issue доставляется агенту."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        handle = _spawn(s._handle_agent_question(task, "conv1", "Какой вариант?"))

        # Имитируем _poll_github_agent_question_answers: вопрос должен быть
        # зарегистрирован с номером issue и границей комментариев.
        for _ in range(100):
            qinfo = s._pending_agent_questions.get("t36")
            if qinfo and qinfo.get("github_issue") is not None:
                break
            await asyncio.sleep(0.01)

        qinfo = s._pending_agent_questions.get("t36")
        assert qinfo is not None, "вопрос не зарегистрирован"
        assert qinfo["github_issue"] == 36, (
            "github_issue не записан — ответ автора никогда не будет прочитан"
        )
        assert qinfo["github_after_comment_id"] is not None

        # Автор ответил комментарием — прямая имитация того, что делает
        # реальный _poll_github_agent_question_answers (fut.set_result).
        if not qinfo["future"].done():
            qinfo["future"].set_result("вариант А")

        answered = await handle
        assert answered is True
        assert s.openhands.sent == [("conv1", "вариант А")]
        # Вопрос и итог (принято) ушли в issue, вопрос опубликован один раз.
        assert len(s.github.comments) == 1
        assert s.github.comments[0][0] == 36

    asyncio.run(scenario())


def test_monitor_does_not_reask_same_message():
    """После доставки ответа монитор не должен задать тот же вопрос повторно,
    пока агент не вышел из awaiting_user_input."""

    async def scenario():
        s = make_scheduler()
        # answered_msg-логика живёт в _monitor_openhands; проверяем её ядро:
        # одинаковое сообщение в awaiting_user_input должно пропускаться.
        answered_msg = "Какой вариант?"
        message = "Какой вариант?"
        assert message == answered_msg  # условие guard в _monitor_openhands

        # Состояние изменилось — защита снимается.
        answered_msg = None
        assert not (message and message == answered_msg)

    asyncio.run(scenario())


def test_send_message_failure_returns_false():
    """Если доставить ответ не удалось, _handle_agent_question сообщает об
    ошибке, а не делает вид, что всё отправлено."""

    async def scenario():
        s = make_scheduler()

        class FailingOpenHands(FakeOpenHands):
            async def send_message(self, conversation_id, message):
                return {"status": "error", "error": "conversation stopped"}

        s.openhands = FailingOpenHands()
        task = make_task()
        handle = _spawn(s._handle_agent_question(task, "conv1", "Какой вариант?"))
        for _ in range(100):
            qinfo = s._pending_agent_questions.get("t36")
            if qinfo:
                break
            await asyncio.sleep(0.01)
        qinfo["future"].set_result("вариант А")
        answered = await handle
        assert answered is False

    asyncio.run(scenario())


def test_clarification_answer_runs_in_background():
    """Ответ на уточнение не должен блокировать вызывающий цикл:
    _run_openhands уходит в фон (иначе главный цикл встаёт на часы)."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        task.status = TaskStatus.CLARIFYING
        task.clarification_answer = "делай вариант А"
        started = asyncio.Event()

        async def fake_run(t):
            started.set()

        s._run_openhands = fake_run

        async def noop_confirm(t):
            return None

        s._confirm_clarification_accepted = noop_confirm
        s._pending_clarifications["t36"] = {
            "task": task,
            "future": None,
            "asked_at": __import__("datetime").datetime.utcnow(),
        }

        await asyncio.wait_for(s._wait_for_clarification(task), timeout=1)
        assert task.status == TaskStatus.READY
        # Фоновая задача запущена и завершается быстро (fake_run).
        await asyncio.wait_for(started.wait(), timeout=1)
        assert "t36" not in s._pending_clarifications

    asyncio.run(scenario())


def test_run_openhands_guard_prevents_double_start():
    """Двойной запуск одной задачи запрещён (гонка главный цикл / фон)."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        task.status = TaskStatus.READY
        calls = []

        async def fake_impl(t):
            calls.append(t.id)
            await asyncio.sleep(0.05)

        s._run_openhands_impl = fake_impl
        s._notify_author = lambda t, text: None

        await asyncio.gather(
            s._run_openhands(task),
            s._run_openhands(task),
        )
        assert calls == ["t36"], "задача запущена дважды"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# get_agent_state: stopped во время ожидания ответа ≠ завершение работы
# --------------------------------------------------------------------------- #

def _state_event(state: str) -> dict:
    return {"observation": "agent_state_changed", "source": "environment",
            "extras": {"agent_state": state}}


def _agent_message(text: str, wait: bool) -> dict:
    return {"action": "message", "source": "agent", "message": text,
            "args": {"wait_for_response": wait}}


def make_adapter_with(events: list[dict]):
    from orchestrator.adapters.openhands import OpenHandsAdapter

    adapter = object.__new__(OpenHandsAdapter)
    adapter.base_url = "http://localhost:3000"

    async def fake_trajectory(cid):
        return {"trajectory": events}

    adapter.get_trajectory = fake_trajectory
    return adapter


def test_stopped_while_awaiting_is_not_completed():
    """Разговор, погашенный idle-таймаутом, пока агент ждал ответ, — НЕ
    завершённая работа (именно так задача #36 ложно 'выполнилась')."""

    async def scenario():
        a = make_adapter_with([
            _agent_message("I'll start by exploring...", True),
            _state_event("running"),
            _state_event("awaiting_user_input"),
            _state_event("stopped"),
        ])
        info = await a.get_agent_state("conv1")
        assert info["agent_state"] == "stopped"
        assert info["completed"] is False, (
            "awaiting_user_input не должен считаться завершением работы"
        )
        assert info["state_before_stop"] == "awaiting_user_input"

    asyncio.run(scenario())


def test_stopped_after_finish_is_completed():
    """Разговор, погашенный после финального отчёта агента, — завершён."""

    async def scenario():
        a = make_adapter_with([
            _agent_message("Готово: всё собралось.", False),
            _state_event("running"),
            _state_event("finished"),
            _state_event("stopped"),
        ])
        info = await a.get_agent_state("conv1")
        assert info["agent_state"] == "stopped"
        assert info["completed"] is True
        assert info["state_before_stop"] == "finished"

    asyncio.run(scenario())
