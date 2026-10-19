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
        self._conn.row_factory = sqlite3.Row
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
        # Миграция для БД, созданных до появления conversation_id / model / attempt.
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "conversation_id" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
        if "model" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        if "attempt" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
        for col in ("tokens_prompt", "tokens_completion", "tokens_cache_read"):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if "cost_rub" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN cost_rub REAL NOT NULL DEFAULT 0")
        if "ui_message_id" not in existing:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN ui_message_id INTEGER")
        # Ключ-значение для настроек UI (например, модель по умолчанию).
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        # Цены моделей (USD за 1 токен) — обновляются периодически из provod.ai.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_prices (
                model TEXT PRIMARY KEY,
                prompt_usd REAL NOT NULL DEFAULT 0,
                completion_usd REAL NOT NULL DEFAULT 0,
                cache_read_usd REAL NOT NULL DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        # Метрики SWE-bенчмарков (справочные, из отчёта цена/качество).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_benchmarks (
                model TEXT PRIMARY KEY,
                swe_bench REAL,
                note TEXT,
                updated_at TEXT
            )
            """
        )
        self._conn.commit()

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Цены моделей (USD за 1 токен)
    # ------------------------------------------------------------------ #
    def upsert_price(self, model: str, prompt_usd: float, completion_usd: float,
                     cache_read_usd: float) -> None:
        from datetime import datetime
        self._conn.execute(
            "INSERT OR REPLACE INTO model_prices "
            "(model, prompt_usd, completion_usd, cache_read_usd, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (model, prompt_usd, completion_usd, cache_read_usd, datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    def get_price(self, model: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM model_prices WHERE model = ?", (model,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_prices(self) -> dict[str, dict]:
        rows = self._conn.execute("SELECT * FROM model_prices").fetchall()
        return {r["model"]: dict(r) for r in rows}

    # ------------------------------------------------------------------ #
    # Метрики SWE-бенчмарков (справочные)
    # ------------------------------------------------------------------ #
    def upsert_benchmark(self, model: str, swe_bench: float | None,
                         note: str | None = None) -> None:
        from datetime import datetime
        self._conn.execute(
            "INSERT OR REPLACE INTO model_benchmarks (model, swe_bench, note, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (model, swe_bench, note, datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    def get_benchmark(self, model: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM model_benchmarks WHERE model = ?", (model,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_benchmarks(self) -> dict[str, dict]:
        rows = self._conn.execute("SELECT * FROM model_benchmarks").fetchall()
        return {r["model"]: dict(r) for r in rows}

    def save(self, task: Task) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, source, source_id, title, description, status,
             clarification_question, clarification_answer, created_at, pr_url, log,
             conversation_id, model, attempt, tokens_prompt, tokens_completion,
             tokens_cache_read, cost_rub, ui_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                task.model,
                task.attempt,
                task.tokens_prompt,
                task.tokens_completion,
                task.tokens_cache_read,
                task.cost_rub,
                task.ui_message_id,
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

    def list_recent(self, limit: int = 50) -> list[Task]:
        """Все задачи, новые сверху — для просмотра истории в /tasks."""
        rows = self._conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_for_chat(self, chat_id: str, limit: int = 50) -> list[Task]:
        """Задачи конкретного чата (Telegram), новые сверху."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE source = ? AND source_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (TaskSource.TELEGRAM.value, chat_id, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        import datetime as _dt

        log_list = []
        if row["log"]:
            try:
                log_list = ast.literal_eval(row["log"])
            except (ValueError, SyntaxError):
                log_list = []

        def _num(key, default=0):
            v = row[key]
            return default if v is None else v

        return Task(
            id=row["id"],
            source=TaskSource(row["source"]),
            source_id=row["source_id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            clarification_question=row["clarification_question"],
            clarification_answer=row["clarification_answer"],
            created_at=_dt.datetime.fromisoformat(row["created_at"]),
            pr_url=row["pr_url"],
            log=log_list,
            conversation_id=row["conversation_id"],
            model=row["model"],
            attempt=_num("attempt", 1),
            tokens_prompt=_num("tokens_prompt"),
            tokens_completion=_num("tokens_completion"),
            tokens_cache_read=_num("tokens_cache_read"),
            cost_rub=_num("cost_rub"),
            ui_message_id=row["ui_message_id"],
        )

    def close(self) -> None:
        self._conn.close()
