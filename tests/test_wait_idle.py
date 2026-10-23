"""Тесты deploy/wait_idle.py: занятость по БД и GHA-маркерам."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import wait_idle  # noqa: E402


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "orchestrator.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    return path


def _insert(db_path: Path, task_id: str, status: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (task_id, status))
    conn.commit()
    conn.close()


class TestBusyTasks:
    def test_empty_db_idle(self, db: Path) -> None:
        assert wait_idle.busy_tasks(db) == []

    def test_missing_db_idle(self, tmp_path: Path) -> None:
        assert wait_idle.busy_tasks(tmp_path / "nope.sqlite") == []

    @pytest.mark.parametrize("status", ["new", "ready", "clarifying", "running"])
    def test_active_statuses_busy(self, db: Path, status: str) -> None:
        _insert(db, "t1", status)
        busy = wait_idle.busy_tasks(db)
        assert busy == [{"id": "t1", "status": status}]

    @pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
    def test_final_statuses_idle(self, db: Path, status: str) -> None:
        _insert(db, "t1", status)
        assert wait_idle.busy_tasks(db) == []


class TestBusyWatchers:
    def test_no_dir_idle(self, tmp_path: Path) -> None:
        assert wait_idle.busy_watchers(tmp_path / "gha_watchers") == []

    def test_fresh_marker_busy(self, tmp_path: Path) -> None:
        d = tmp_path / "gha_watchers"
        d.mkdir()
        (d / "123.watch").touch()
        assert wait_idle.busy_watchers(d) == [{"id": "123", "age_min": 0}]

    def test_stale_marker_ignored(self, tmp_path: Path) -> None:
        d = tmp_path / "gha_watchers"
        d.mkdir()
        marker = d / "123.watch"
        marker.touch()
        old = time.time() - wait_idle.STALE_WATCHER_SECONDS - 60
        os.utime(marker, (old, old))
        assert wait_idle.busy_watchers(d) == []

    def test_other_files_ignored(self, tmp_path: Path) -> None:
        d = tmp_path / "gha_watchers"
        d.mkdir()
        (d / "notes.txt").touch()
        assert wait_idle.busy_watchers(d) == []


class TestSnapshot:
    def test_idle_when_both_empty(self, tmp_path: Path) -> None:
        tasks, watchers = wait_idle.snapshot(
            tmp_path / "state" / "db.sqlite", tmp_path / "state" / "gha_watchers"
        )
        assert tasks == [] and watchers == []
