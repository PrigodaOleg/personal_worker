"""Кнопка «Подтолкнуть» для задачи с красным PR: возобновление CI-автофикса.

Пользовательская проблема (PR #53/#54): лимит раундов автоисправления
исчерпан — PR остался красным, задача «зависла» в done без шансов на
починку. Кнопка «📤 Подтолкнуть» в карточке отправляет логи последнего
упавшего прогона агенту и перезапускает GHA-вотчер.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


def make_task(status=TaskStatus.DONE) -> Task:
    return Task(
        id="tk-ci",
        source=TaskSource.GITHUB,
        source_id="54",
        title="e2e на ios",
        description="",
        status=status,
        pr_url="https://github.com/o/r/pull/54",
        conversation_id="conv-1",
    )


def _coro(result):
    async def _fn(*args, **kwargs):
        return result
    return _fn


def test_can_resume_ci():
    s = object.__new__(Scheduler)
    t = make_task()
    assert s._can_resume_ci(t)
    # ci_pending — вотчер уже жив, кнопка не нужна
    t.ci_pending = True
    assert not s._can_resume_ci(t)
    t.ci_pending = False
    # без PR / без разговора — нельзя
    t.pr_url = None
    assert not s._can_resume_ci(t)
    t.pr_url = "https://github.com/o/r/pull/54"
    t.conversation_id = None
    assert not s._can_resume_ci(t)
    # выполняющаяся задача — не для этой кнопки
    t.conversation_id = "conv-1"
    t.status = TaskStatus.RUNNING
    assert not s._can_resume_ci(t)


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


def test_resume_retriggers_on_cancelled_run():
    """Отменённый прогон: правки уже в ветке → пустой коммит → новый прогон."""
    s = object.__new__(Scheduler)
    spawned: list = []
    s.github_for = lambda repo=None: types.SimpleNamespace(
        fetch_pr_head_branch=lambda url: "ai-task-tk-ci-r1",
        last_bot_comment_id=lambda issue: 77,
        find_workflow_run=lambda sha, t=40, p=8.0: {
            "id": 43, "html_url": "https://ci/43", "conclusion": None,
        },
        get_git_credentials=lambda: ("pw-bot", "fake-token"),
    )
    s._repo_full = lambda task: "o/r"
    s._find_latest_ci_run = _coro(  # type: ignore[attr-defined]
        {"id": 42, "html_url": "https://ci/42", "conclusion": "cancelled"}
    )
    s._git_head_sha = _coro("abc123")  # type: ignore[attr-defined]
    s.config = types.SimpleNamespace(
        git_author_name="pw", git_author_email="pw@x",
    )
    saved: list = []
    s.state = types.SimpleNamespace(save=lambda task: saved.append(task.id))
    s._ensure_workspace = lambda task: "/tmp/pw-ws"
    s._spawn = spawned.append

    ran: list = []

    class FakeProc:
        returncode = 0
        stderr = b""

        async def communicate(self, timeout=None):
            return (b"", b"")

    async def fake_exec(*cmd, **kwargs):
        ran.append(cmd)
        return FakeProc()

    import orchestrator.core.scheduler as sched
    orig_exec = sched.asyncio.create_subprocess_exec
    orig_wait = sched.asyncio.wait_for

    async def fake_wait_for(proc, timeout=None):
        # git-команды _retrigger_ci — FakeProc; корутины (_git_head_sha) —
        # пробрасываем в настоящий wait_for.
        if isinstance(proc, FakeProc):
            return await proc.communicate()
        return await orig_wait(proc, timeout)

    sched.asyncio.create_subprocess_exec = fake_exec
    sched.asyncio.wait_for = fake_wait_for
    try:
        result = asyncio.run(s._resume_ci_loop(make_task()))
    finally:
        sched.asyncio.create_subprocess_exec = orig_exec
        sched.asyncio.wait_for = orig_wait
    assert "отменён" in result
    # пустой коммит + пуш в ветку PR
    assert any("commit" in c for c in ran) and any("push" in c for c in ran)
    assert spawned
    locs = spawned[0].cr_frame.f_locals
    assert locs["run_id"] == 43
    spawned[0].close()

    """Результат возобновления уходит автору задачи."""
    s = object.__new__(Scheduler)
    s._resume_ci_loop = _coro("🔄 CI-цикл возобновлён")  # type: ignore
    notified: list = []
    s._notify_author = lambda task, text: notified.append(text)  # type: ignore
    s._notify_admin = lambda text: None  # type: ignore
    s._repo_full = lambda task: "o/r"
    t = make_task()
    asyncio.run(s._resume_ci_and_report(t))
    assert notified == ["🔄 CI-цикл возобновлён"]


def test_keyboard_shows_nudge_for_red_pr_task():
    """У задачи с PR и красным CI есть кнопка «📤 Подтолкнуть»."""
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}
    s._killed_by_openhands = lambda task: False
    s.telegram = types.SimpleNamespace(build_keyboard=lambda rows: rows)
    t = make_task()
    kb = s._task_actions_keyboard(t)
    texts = [btn[0] for row in kb for btn in row]
    assert "📤 Подтолкнуть" in texts
