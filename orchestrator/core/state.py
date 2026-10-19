"""Хранение состояния задач в SQLite."""

from __future__ import annotations

import sqlite3
import ast
from pathlib import Path

from orchestrator.core.task import Task, TaskSource, TaskStatus


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                clarification_question TEXT,
                clarification_answer TEXT,
                created_at TEXT NOT NULL,
                pr_url TEXT,
                log TEXT NOT NULL DEFAULT '[]',
                conversation_id TEXT
            )
            """
        )
        # Миграция для БД, созданных до появления conversation_id.
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "conversation_id" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
        self._conn.commit()

    def save(self, task: Task) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, source, source_id, title, description, status,
             clarification_question, clarification_answer, created_at, pr_url, log,
             conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.source.value,
                task.source_id,
                task.title,
                task.description,
                task.status.value,
                task.clarification_question,
                task.clarification_answer,
                task.created_at.isoformat(),
                task.pr_url,
                str(task.log),
                task.conversation_id,
            ),
        )
        self._conn.commit()

    def get(self, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def list_by_status(self, status: TaskStatus) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at",
            (status.value,),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row: tuple) -> Task:
        import datetime as _dt

        log_list = []
        if row[10]:
            try:
                log_list = ast.literal_eval(row[10])
            except (ValueError, SyntaxError):
                log_list = []

        return Task(
            id=row[0],
            source=TaskSource(row[1]),
            source_id=row[2],
            title=row[3],
            description=row[4],
            status=TaskStatus(row[5]),
            clarification_question=row[6],
            clarification_answer=row[7],
            created_at=_dt.datetime.fromisoformat(row[8]),
            pr_url=row[9],
            log=log_list,
            conversation_id=row[11] if len(row) > 11 else None,
        )

    def close(self) -> None:
        self._conn.close()
