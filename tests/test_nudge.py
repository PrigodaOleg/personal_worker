"""Кнопка «Подтолкнуть»: продолжение задачи, упавшей после работы агента.

Агент уже сделал работу, но финализация (ветка → пуш → PR) упала — например,
GitHub отверг пуш workflow-файла из-за нехватки прав. Перезапуск агента
неоправданно дорог; вместо этого флаг finalize_only отправляет задачу сразу
на финализацию из готовой рабочей копии.
"""

import sqlite3

import pytest

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.state import StateStore
from orchestrator.core.task import Task, TaskSource, TaskStatus


def make_task(status: TaskStatus = TaskStatus.FAILED, log: list[str] | None = None,
              pr_url: str | None = None) -> Task:
    t = Task(
        id="nudgetest1",
        source=TaskSource.TELEGRAM,
        source_id="42",
        title="Тестовая задача",
        description="описание",
        status=status,
        pr_url=pr_url,
    )
    for line in (log or []):
        t.add_log(line)
    return t


def test_failed_after_push_is_nudgeable():
    t = make_task(log=["git/push failed: remote rejected"])
    assert Scheduler._can_nudge_finalization(t)


def test_failed_after_sync_is_nudgeable():
    t = make_task(log=["Из контейнера: изменено 5, удалено 0",
                       "Синхронизация из контейнера не удалась: boom"])
    assert Scheduler._can_nudge_finalization(t)


def test_failed_before_agent_work_is_not_nudgeable():
    # Упала на старте агента — полный перезапуск, не подталкивание.
    t = make_task(log=["OpenHands не создал разговор: timeout"])
    assert not Scheduler._can_nudge_finalization(t)


def test_running_task_is_not_nudgeable():
    t = make_task(status=TaskStatus.RUNNING,
                  log=["Из контейнера: изменено 5, удалено 0"])
    assert not Scheduler._can_nudge_finalization(t)


def test_failed_with_pr_is_not_nudgeable():
    t = make_task(log=["git/push failed"], pr_url="https://github.com/x/pull/1")
    assert not Scheduler._can_nudge_finalization(t)


def test_done_without_pr_is_nudgeable():
    # Пуш прошёл, create_pr упал — задача осталась done без PR.
    t = make_task(status=TaskStatus.DONE,
                  log=["OpenHands finished", "Из контейнера: изменено 3, удалено 0"])
    assert Scheduler._can_nudge_finalization(t)


def test_done_with_pr_is_not_nudgeable():
    t = make_task(status=TaskStatus.DONE,
                  log=["Из контейнера: изменено 3, удалено 0"],
                  pr_url="https://github.com/x/pull/2")
    assert not Scheduler._can_nudge_finalization(t)


def test_start_nudge_sets_running_and_flag(tmp_path):
    sched = object.__new__(Scheduler)
    sched.state = StateStore(tmp_path / "state.sqlite")
    t = make_task(log=["git/push failed"])
    sched.state.save(t)

    sched._start_nudge(t)

    fresh = sched.state.get(t.id)
    assert fresh.status == TaskStatus.RUNNING
    assert fresh.finalize_only is True
    assert "Подталкивание" in fresh.log[-1]


def test_finalize_only_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite")
    t = make_task()
    t.finalize_only = True
    store.save(t)
    fresh = store.get(t.id)
    assert fresh.finalize_only is True


def test_finalize_only_migration(tmp_path):
    """БД старой схемы (без finalize_only) должна мигрировать при открытии."""
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, source TEXT, source_id TEXT, "
        "title TEXT, description TEXT, status TEXT, clarification_question TEXT, "
        "clarification_answer TEXT, created_at TEXT, pr_url TEXT, log TEXT)"
    )
    conn.commit()
    conn.close()

    store = StateStore(db)
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(tasks)")}
    assert "finalize_only" in cols

    t = make_task()
    t.finalize_only = True
    store.save(t)
    assert store.get(t.id).finalize_only is True


@pytest.mark.parametrize("marker", [
    "Из контейнера: изменено 5, удалено 0",
    "git/push failed: boom",
    "Синхронизация из контейнера не удалась: boom",
    "Финализация упала: boom",
])
def test_all_failure_markers_recognized(marker):
    t = make_task(log=[marker])
    assert Scheduler._can_nudge_finalization(t), marker
