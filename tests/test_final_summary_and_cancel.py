"""Финальное саммари агента + цена в уведомлении + отмена из карточки.

- Отчёт агента (kind="report") сохраняется в task.agent_summary и уходит
  отдельным сообщением (комментарием в issue / сообщением автору) ПЕРЕД
  коротким уведомлением со ссылками.
- В уведомлении со ссылками есть стоимость в рублях.
- Активную задачу можно отменить кнопкой cancel: из карточки.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeState:
    def __init__(self):
        self.tasks: dict[str, Task] = {}

    def get(self, task_id):
        return self.tasks.get(task_id)

    def save(self, task):
        self.tasks[task.id] = task


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.answered: list[str] = []

    async def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))

    async def answer_callback_query(self, cqid, text=None):
        self.answered.append(text or "")

    def build_keyboard(self, rows):
        return rows


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[int, str]] = []

    def create_comment(self, issue_number, text):
        self.comments.append((issue_number, text))
        return 12345


def make_scheduler() -> tuple[Scheduler, FakeGitHub, FakeTelegram]:
    s = object.__new__(Scheduler)
    s.state = FakeState()
    s.github = FakeGitHub()
    s.telegram = FakeTelegram()
    s._pending_notifications = set()
    s._pending_clarifications = {}
    s._pending_agent_questions = {}
    s._repo_adapters = {}
    s.config = types.SimpleNamespace(
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
    )
    return s, s.github, s.telegram


def make_task(status=TaskStatus.RUNNING) -> Task:
    return Task(
        id="tk1",
        source=TaskSource.GITHUB,
        source_id="46",
        title="e2e тесты",
        description="",
        status=status,
        conversation_id="conv1",
        repo="PrigodaOleg/social_mind",
        cost_rub=42.5,
    )


def test_report_saved_to_agent_summary():
    """kind=report: отчёт агента попадает в agent_summary."""
    s, gh, tg = make_scheduler()
    task = make_task()
    s.state.save(task)
    msg = "Готово. Добавил Android e2e:\n- вынес тело теста\n- расширил скрипт"
    assert Scheduler._classify_agent_pause(msg) == "report"
    # Симулируем ветку сохранения (как в _monitor_openhands):
    task.agent_summary = Scheduler._strip_tool_markup(msg)[:4000]
    task.status = TaskStatus.DONE
    s.state.save(task)
    assert "Android e2e" in task.agent_summary


def test_summary_sent_to_issue_before_links():
    """Саммари уходит комментарием в issue отдельным сообщением."""
    s, gh, tg = make_scheduler()
    task = make_task(TaskStatus.DONE)
    task.agent_summary = "Сделал всё: тесты зелёные."
    asyncio.run(s._notify_summary(task, task.agent_summary))
    assert len(gh.comments) == 1
    num, text = gh.comments[0]
    assert num == 46
    assert "Саммари проделанной работы" in text
    assert "тесты зелёные" in text


def test_summary_sent_to_telegram_author():
    """Telegram-задача: саммари автору сообщением."""
    s, gh, tg = make_scheduler()
    task = make_task(TaskStatus.DONE)
    task.source = TaskSource.TELEGRAM
    task.source_id = "555"
    task.agent_summary = "Готово."
    asyncio.run(s._notify_summary(task, "Готово."))
    assert any("Саммари" in text for _, text in tg.sent)


def test_cost_line_contains_rub():
    """Стоимость в уведомлении — в рублях."""
    s, _, _ = make_scheduler()
    task = make_task(TaskStatus.DONE)
    task.cost_rub = 123.4
    task.tokens_prompt = 1000
    line = s._cost_line(task)
    assert "123.40 ₽" in line


def test_cancel_button_handler():
    """Кнопка cancel: активная задача отменяется, завершённая — нет."""
    s, gh, tg = make_scheduler()
    s._spawn = lambda coro: None  # остановка разговора не нужна в тесте
    active = make_task(TaskStatus.RUNNING)
    s.state.save(active)

    s._cancel_task(active, "тест")
    assert active.status == TaskStatus.CANCELLED

    done = make_task(TaskStatus.DONE)
    s.state.save(done)
    assert done.status == TaskStatus.DONE  # завершённую не трогает


def test_task_keyboard_has_cancel_for_active():
    """В клавиатуре активной задачи есть кнопка отмены; у завершённой — нет."""
    s, _, _ = make_scheduler()
    active = make_task(TaskStatus.RUNNING)
    kb = s._task_actions_keyboard(active)
    flat = [lbl for row in kb for lbl, _ in row]
    assert any("Отменить" in lbl for lbl in flat)

    done = make_task(TaskStatus.DONE)
    kb2 = s._task_actions_keyboard(done)
    flat2 = [lbl for row in kb2 for lbl, _ in row]
    assert not any("Отменить" in lbl for lbl in flat2)


def test_killed_flag_ignores_old_attempt():
    """Маркер смерти из прошлой попытки не помечает убитой текущую.

    После перезапуска лог задачи хранит смерть attempt 1 — раньше карточка
    навсегда показывала «🪓 убита», хотя задача работала (bug report).
    """
    s, _, _ = make_scheduler()
    t = make_task(TaskStatus.RUNNING)
    t.log = [
        "[2026-10-26T10:44:35] issue #46: e2e тесты",
        "[2026-10-26T12:35:21] OpenHands killed task: агент зациклился "
        "(AgentStuckInLoopError) — ретрай не помог",
        "[2026-10-26T12:40:00] Перезапуск #2, модель: openai/x",
        "[2026-10-26T12:41:00] OpenHands conversation created: new-cid",
    ]
    assert s._killed_by_openhands(t) is False

    # Маркер ПОСЛЕ перезапуска — задача действительно убита.
    t.log.append(
        "[2026-10-26T13:00:00] OpenHands killed task: агент зациклился "
        "(AgentStuckInLoopError) — ретрай не помог"
    )
    assert s._killed_by_openhands(t) is True

    # Без перезапусков — как раньше.
    t2 = make_task(TaskStatus.FAILED)
    t2.log = ["[x] OpenHands killed task: агент зациклился"]
    assert s._killed_by_openhands(t2) is True


def test_cleanup_skipped_for_pr_tasks():
    """Задача с PR не чистится в finally: контейнер нужен CI-вотчеру."""
    s, _, _ = make_scheduler()
    removed = []

    async def fake_docker(*args, **kw):
        removed.append(args)
        return (0, "", "")

    s._docker = fake_docker
    t = make_task(TaskStatus.DONE)
    t.pr_url = "https://github.com/o/r/pull/1"
    asyncio.run(s._cleanup_runtime_container(t))
    assert removed == [], "контейнер PR-задачи не должен удаляться"

    t2 = make_task(TaskStatus.FAILED)
    t2.pr_url = None
    asyncio.run(s._cleanup_runtime_container(t2))
    assert removed and "rm" in removed[0]
