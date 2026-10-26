"""Ревью PR → агенту: комментарии/ревью/инлайн попадают в разговор.

Задача в DONE, но жизнь PR продолжается. Опрос (_poll_pr_review_feedback)
берёт новые отзывы живых людей (боты отфильтрованы в адаптере), трекает
«после какого id» в settings БД и отправляет агенту; после ответа агента
оркестратор синхронизирует правки и допушивает в ветку PR.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeState:
    def __init__(self):
        self.settings: dict[str, str] = {}
        self.tasks: dict[str, Task] = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def save(self, task):
        self.tasks[task.id] = task

    def list_by_status(self, status):
        return [t for t in self.tasks.values() if t.status == status]


class FakeGitHub:
    def __init__(self, feedback=None, branch="ai-task-x-r1"):
        self._feedback = feedback or []
        self._branch = branch
        self.feedback_calls: list[tuple[str, int]] = []

    def fetch_pr_feedback(self, pr_url, after_comment_id=0):
        self.feedback_calls.append((pr_url, after_comment_id))
        return self._feedback

    def fetch_pr_head_branch(self, pr_url):
        return self._branch

    def last_bot_comment_id(self, issue_number):
        return 42


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


def make_scheduler(feedback=None) -> Scheduler:
    s = object.__new__(Scheduler)
    s.state = FakeState()
    s.github = FakeGitHub(feedback)
    s.telegram = FakeTelegram()
    s.openhands = types.SimpleNamespace()
    s._repo_adapters = {}
    s._background_tasks = set()
    s._pending_notifications = set()
    s._notify_author_calls: list = []
    # Для тестов: троттлинг опроса отключён.
    s.review_poll_min_interval = 0
    # Допуш в тестах не выполняем: правок в контейнере нет.
    async def no_push(*a, **kw):
        return None
    s._sync_and_push_ci_fix = no_push
    s.config = types.SimpleNamespace(
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
        work_dir="/tmp/pw-test-workspaces",
    )

    def notify_author(task, text):
        s._notify_author_calls.append(text)

    s._notify_author = notify_author
    s._spawn = lambda coro: None  # фоновые задачи не запускаем
    return s


def make_done_task() -> Task:
    t = Task(
        id="tk9",
        source=TaskSource.GITHUB,
        source_id="48",
        title="e2e тесты",
        description="",
        status=TaskStatus.DONE,
        conversation_id="conv9",
        repo="PrigodaOleg/social_mind",
    )
    t.pr_url = "https://github.com/PrigodaOleg/social_mind/pull/49"
    return t


def test_poll_forwards_feedback_to_agent():
    feedback = [
        {"id": 10, "author": "alice", "kind": "issue_comment",
         "body": "Пожалуйста, добавь тест на пустой ввод."},
        {"id": 11, "author": "bob", "kind": "inline", "path": "lib/x.dart",
         "body": "Лишний println."},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def capture(cid, message):
        s.captured = message
        return {"success": True}

    s.openhands.send_message = capture
    s._poll_pr_review_feedback()
    # _spawn заглушён — вызовем хендлер вручную.
    asyncio.run(s._handle_pr_feedback(task, feedback))

    assert "ПУШИТЬ НЕ НУЖНО" in s.captured
    assert "добавь тест на пустой ввод" in s.captured


def test_feedback_state_tracked_in_settings():
    """После обработки «после какого id» сохранён — повторный poll пуст."""
    feedback = [
        {"id": 10, "author": "alice", "kind": "issue_comment", "body": "fix"},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)
    s.state.set_setting("pr_review_last_poll", "0")

    s._poll_pr_review_feedback()
    key = "pr_review_last_id:tk9"
    assert s.state.get_setting(key) == "10"

    # Новых комментариев нет → после первого опроса feedback отдаётся пустой.
    s.github._feedback = []
    s._poll_pr_review_feedback()
    assert s.github.feedback_calls[-1] == (task.pr_url, 10)


def test_review_message_contains_author_path_and_instruction():
    feedback = [
        {"id": 10, "author": "alice", "kind": "review:changes_requested",
         "body": "Не хватает тестов."},
        {"id": 11, "author": "bob", "kind": "inline", "path": "a/b.dart",
         "body": "Переименуй переменную."},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()

    async def capture(cid, message):
        s.captured = message
        return {"success": True}

    s.openhands.send_message = capture
    asyncio.run(s._handle_pr_feedback(task, feedback))
    msg = s.captured
    assert "review:changes_requested от @alice" in msg
    assert "inline от @bob (a/b.dart)" in msg
    assert "ПУШИТЬ НЕ НУЖНО" in msg
    assert "Не хватает тестов." in msg


def test_tasks_without_pr_skipped():
    s = make_scheduler([])
    t = make_done_task()
    t.pr_url = "(dry-run) ветка x не отправлена"
    s.state.save(t)
    s._poll_pr_review_feedback()
    assert s.github.feedback_calls == []
