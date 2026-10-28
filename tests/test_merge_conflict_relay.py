"""Механизм доставки merge-конфликтов PR воркеру.

Воркер не видит GitHub: оркестратор проверяет mergeable_state, локально
сливает base-ветку, конфликтные файлы кладёт в контейнер и просит воркера
разрулить. Пушил по-прежнему оркестратор.
"""

from __future__ import annotations

import asyncio
import subprocess
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskStatus


def _mk_task(**kw) -> Task:
    t = Task(id=kw.get("id", "t1"), source=kw.get("source") or 0,
             source_id="1", title="x", description="d")
    t.status = TaskStatus.DONE
    t.pr_url = "https://github.com/o/r/pull/9"
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _bare_scheduler() -> Scheduler:
    s = object.__new__(Scheduler)
    s._merge_cycles = {}
    s._merge_rounds = {}
    s.max_merge_fix_rounds = 5
    s.state = types.SimpleNamespace(save=lambda t: None)
    return s


def test_dirty_state_is_handled_and_clean_is_not():
    assert "dirty" in Scheduler._MERGE_DIRTY_STATES
    assert "behind" in Scheduler._MERGE_DIRTY_STATES
    assert "clean" not in Scheduler._MERGE_DIRTY_STATES
    assert "blocked" not in Scheduler._MERGE_DIRTY_STATES


def test_poll_spawns_resolver_on_dirty_and_skips_inflight():
    """dirty → иск в _resolve_merge_conflict; повторный запуск исключён."""
    s = _bare_scheduler()
    task = _mk_task()
    task.merge_conflict = False

    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()  # не исполняем

    s._spawn = fake_spawn
    s.github_for = lambda repo: types.SimpleNamespace(
        get_pr_merge_info=lambda pr_url: {
            "state": "open", "merged": False, "mergeable": False,
            "mergeable_state": "dirty", "base_ref": "main",
            "head_ref": "pr-branch",
        }
    )
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        list_by_status=lambda st: [task] if st is TaskStatus.DONE else [],
    )

    asyncio.run(s._poll_pr_merge_conflicts())
    assert len(spawned) == 1, "dirty PR должен запустить резолвер"

    # Пока цикл идёт — повторный поллер не плодит циклы.
    s._merge_cycles[task.id] = True
    asyncio.run(s._poll_pr_merge_conflicts())
    assert len(spawned) == 1


def test_poll_clears_flag_when_mergeable_again():
    """Конфликт разрулили (чистый/merged) — флаг merge_conflict снимается."""
    s = _bare_scheduler()
    task = _mk_task()
    task.merge_conflict = True
    saved: list[Task] = []
    s.state = types.SimpleNamespace(
        save= lambda t: saved.append(t),
        list_by_status=lambda st: [task] if st is TaskStatus.DONE else [],
    )
    s.github_for = lambda repo: types.SimpleNamespace(
        get_pr_merge_info=lambda pr_url: {
            "state": "open", "merged": False, "mergeable": True,
            "mergeable_state": "clean", "base_ref": "main",
            "head_ref": "pr-branch",
        }
    )
    asyncio.run(s._poll_pr_merge_conflicts())
    assert task.merge_conflict is False
    assert saved


def test_poll_skips_merged_and_closed_prs():
    s = _bare_scheduler()
    merged = _mk_task(id="m1")
    closed = _mk_task(id="c1")
    spawned: list = []

    def fake_spawn(coro):
        spawned.append(coro)
        coro.close()

    s._spawn = fake_spawn
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        list_by_status=lambda st: [merged, closed] if st is TaskStatus.DONE else [],
    )

    def info(pr_url: str):
        if pr_url.endswith("9"):
            return {"state": "open", "merged": True}
        return {"state": "closed", "merged": False}

    s.github_for = lambda repo: types.SimpleNamespace(get_pr_merge_info=info)
    asyncio.run(s._poll_pr_merge_conflicts())
    assert not spawned


def test_git_start_merge_reports_conflicts(tmp_path):
    """_git_start_merge: чистый merge → [], конфликт → список файлов."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "a"], check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "pr"], check=True)
    (repo / "f.txt").write_text("pr\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-aqm", "pr"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    (repo / "f.txt").write_text("main\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-aqm", "main"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Предок ветки pr — merge уже актуален: конфликтов нет, список пуст.
    ancestor_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "pr~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "pr"], check=True)

    s = object.__new__(Scheduler)
    # git-авторство подменяем: тестируем только логику merge.
    s._git_auth_args = lambda repo=None: ["-c", "user.name=a", "-c", "user.email=a@b.c"]

    conflicts = asyncio.run(s._git_start_merge(str(repo), base_sha))
    assert conflicts == ["f.txt"], conflicts

    # Чистый (уже актуальный) merge поверх прерванного: abort → пустой список.
    asyncio.run(s._abort_local_merge(str(repo)))
    assert asyncio.run(s._git_start_merge(str(repo), ancestor_sha)) == []


def test_sync_and_push_ci_fix_uses_custom_commit_message():
    """commit_message прокидывается в git commit (merge-резолв и CI-фикс)."""
    import inspect
    src = inspect.getsource(Scheduler._sync_and_push_ci_fix)
    assert "commit_message" in src
    assert 'commit_message\n                or f"AI task: {task.title} (CI fix)"' in src.replace(
        "\n            or", "\n                or"
    ) or "or f\"AI task: {task.title} (CI fix)\"" in src


def test_merge_conflict_message_contains_instructions():
    """Сообщение воркеру объясняет маркеры и запрет пуша."""
    import inspect
    src = inspect.getsource(Scheduler._send_merge_conflict_to_agent)
    assert "ПУШИТЬ НЕ НУЖНО" in src
    assert "<<<<<<<" in src
    assert "_container_ws(task)" in src
