"""Вотчер GHA сообщает о провале/зависании сборки, а не молчит.

Раньше вотчер при провале только обновлял комментарий в issue (автор мог
не заметить), а при зависании — писал одну строку в task.log. Теперь:
- провал/отмена сборки → уведомление автору (TG) + админу (GH-задачи);
- таймаут слежения → уведомление автору и админу со ссылкой на run.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeGitHub:
    def __init__(self, run=None, artifacts=None):
        self._run = run or {}
        self._artifacts = artifacts or []

    def get_workflow_run(self, run_id):
        return self._run

    def list_run_artifacts(self, run_id):
        return self._artifacts

    def update_comment(self, comment_id, body):
        return True


def make_watcher_scheduler(source: TaskSource, run: dict):
    s = object.__new__(Scheduler)
    s.config = types.SimpleNamespace(
        state_db="/tmp/test-pw/watch.sqlite",
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
    )
    s.state = types.SimpleNamespace(save=lambda t: None)
    s.github = FakeGitHub(run=run)
    s._repo_adapters = {}
    s._notify_author_calls: list = []
    s._notify_admin_calls: list = []

    def notify_author(task, text):
        s._notify_author_calls.append(text)

    def notify_admin(text):
        s._notify_admin_calls.append(text)

    s._notify_author = notify_author
    s._notify_admin = notify_admin
    s.gha_watch_poll_seconds = 0.001
    s.gha_watch_timeout_seconds = 0.001  # дедлайн сразу истёк
    return s


def make_task(source: TaskSource) -> Task:
    return Task(
        id="tk3",
        source=source,
        source_id="48" if source == TaskSource.GITHUB else "1",
        title="e2e тесты",
        description="",
        status=TaskStatus.DONE,
        conversation_id="conv3",
    )


def watch(s: Scheduler, task: Task, run_id: int = 40143766467):
    return asyncio.run(
        s._watch_gha_artifacts_loop(task, 111, "body", run_id)
    )


def test_timeout_notifies_author_and_admin():
    s = make_watcher_scheduler(TaskSource.GITHUB, run={})
    task = make_task(TaskSource.GITHUB)
    watch(s, task)
    assert any("не завершилась за время слежения" in t
               for t in s._notify_author_calls)
    assert any("не завершилась за время слежения" in t
               for t in s._notify_admin_calls)
    assert any("actions/runs/40143766467" in t
               for t in s._notify_author_calls + s._notify_admin_calls)


def test_failed_run_notifies_author_and_admin():
    s = make_watcher_scheduler(
        TaskSource.GITHUB,
        run={"status": "completed", "conclusion": "cancelled",
             "html_url": "https://github.com/PrigodaOleg/social_mind/actions/runs/40143766467"},
    )
    s.gha_watch_timeout_seconds = 60  # дедлайн не мешает: run уже completed
    task = make_task(TaskSource.GITHUB)
    watch(s, task)
    assert any("«cancelled»" in t for t in s._notify_author_calls)
    assert any("cancelled" in t for t in s._notify_admin_calls)


def test_success_run_no_kill_notification():
    s = make_watcher_scheduler(
        TaskSource.GITHUB,
        run={"status": "completed", "conclusion": "success",
             "html_url": "https://github.com/PrigodaOleg/social_mind/actions/runs/1"},
    )
    s.gha_watch_timeout_seconds = 60
    task = make_task(TaskSource.GITHUB)
    watch(s, task)
    assert s._notify_admin_calls == []
    assert s._notify_author_calls == []
