"""Команда /ci: возобновление CI-автофикса у задачи с красным PR.

Пользовательская проблема (PR #53/#54): лимит раундов автоисправления
исчерпан — PR остался красным, задача «зависла» в done без шансов на
починку. /ci <task_id> отправляет логи последнего упавшего прогона агенту
и перезапускает GHA-вотчер.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


def make_task() -> Task:
    return Task(
        id="tk-ci",
        source=TaskSource.GITHUB,
        source_id="54",
        title="e2e на ios",
        description="",
        status=TaskStatus.DONE,
        pr_url="https://github.com/o/r/pull/54",
        conversation_id="conv-1",
    )


def _coro(result):
    async def _fn(*args, **kwargs):
        return result
    return _fn


def test_resume_skips_without_pr():
    s = object.__new__(Scheduler)
    t = make_task()
    t.pr_url = None
    assert "нет PR" in asyncio.run(s._resume_ci_loop(t))


def test_resume_skips_without_conversation():
    s = object.__new__(Scheduler)
    t = make_task()
    t.conversation_id = None
    assert "Разговор" in asyncio.run(s._resume_ci_loop(t))


def test_resume_skips_on_green_run():
    """Зелёный CI — трогать нечего, агенту ничего не отправляем."""
    s = object.__new__(Scheduler)
    s.github_for = lambda repo=None: types.SimpleNamespace(
        fetch_pr_head_branch=lambda url: "ai-task-tk-ci-r1",
    )
    s._repo_full = lambda task: "o/r"
    s._find_latest_ci_run = _coro(  # type: ignore[attr-defined]
        {"id": 1, "html_url": "u", "conclusion": "success"}
    )
    t = make_task()
    result = asyncio.run(s._resume_ci_loop(t))
    assert "уже зелёный" in result
    assert not t.ci_pending


def test_resume_spawns_watcher_on_failure():
    """Красный прогон → логи агенту, ci_pending=True, вотчер заспавнен."""
    s = object.__new__(Scheduler)
    spawned: list = []
    s.github_for = lambda repo=None: types.SimpleNamespace(
        fetch_pr_head_branch=lambda url: "ai-task-tk-ci-r1",
        last_bot_comment_id=lambda issue: 77,
    )
    s._repo_full = lambda task: "o/r"
    s._find_latest_ci_run = _coro(  # type: ignore[attr-defined]
        {"id": 42, "html_url": "https://ci/42", "conclusion": "failure"}
    )
    s._send_ci_failure_to_agent = _coro(True)  # type: ignore[attr-defined]
    saved: list = []
    s.state = types.SimpleNamespace(save=lambda task: saved.append(task.id))
    s._ensure_workspace = lambda task: "/tmp/pw-ws"
    s._spawn = spawned.append
    t = make_task()
    result = asyncio.run(s._resume_ci_loop(t))
    assert "возобновлён" in result
    assert t.ci_pending
    assert spawned, "вотчер должен быть заспавнен"
    locs = spawned[0].cr_frame.f_locals
    assert locs["run_id"] == 42
    assert locs["client_dir"].endswith("social_mind")
    assert locs["branch"] == "ai-task-tk-ci-r1"
    assert locs["comment_id"] == 77
    spawned[0].close()
