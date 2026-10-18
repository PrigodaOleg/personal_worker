"""Модель задачи и её жизненный цикл."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskSource(str, Enum):
    GITHUB = "github"
    TELEGRAM = "telegram"


class TaskStatus(str, Enum):
    NEW = "new"
    CLARIFYING = "clarifying"  # ждём ответ пользователя
    READY = "ready"            # можно запускать OpenHands
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"    # отменено пользователем


@dataclass
class Task:
    id: str
    source: TaskSource
    source_id: str  # номер issue или ID сообщения Telegram
    title: str
    description: str
    status: TaskStatus = TaskStatus.NEW
    clarification_question: Optional[str] = None
    clarification_answer: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    pr_url: Optional[str] = None
    log: list[str] = field(default_factory=list)

    def add_log(self, message: str) -> None:
        self.log.append(f"[{datetime.utcnow().isoformat()}] {message}")
