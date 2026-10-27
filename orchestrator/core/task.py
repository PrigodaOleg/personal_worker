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
    # ID разговора OpenHands. Нужен, чтобы найти runtime-контейнер задачи
    # (он называется openhands-runtime-<conversation_id>) и показать его логи.
    conversation_id: Optional[str] = None
    # Модель LLM для этой задачи. None — использовать модель по умолчанию.
    # Можно переопределить из /tasks, в том числе у упавшей задачи перед рестартом.
    model: Optional[str] = None
    # Номер попытки. Растёт при перезапуске, чтобы ветка PR не конфликтовала
    # с веткой предыдущего прогона.
    attempt: int = 1
    # Токены, израсходованные моделью (из llm_metrics OpenHands), и цена работы.
    # Цена считается как токены × сохранённые цены модели (см. pricing.py).
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cache_read: int = 0
    cost_rub: float = 0.0
    # ID сообщения с кнопками (меню/карточкой), чтобы обновлять его in-place.
    ui_message_id: Optional[int] = None
    # Разовый флаг «подталкивания»: пропустить агента и сразу финализировать
    # (ветка → пуш → PR) из уже готовой рабочей копии. Ставится кнопкой
    # «Подтолкнуть», когда задача упала после работы агента — на коммите/пуше.
    finalize_only: bool = False
    # Репозиторий-источник (owner/name). None — репозиторий по умолчанию
    # (config.github_repo). Поддерживается несколько репозиториев: каждый
    # issue обрабатывается в своём репо (PR, комментарии, артефакты GHA).
    repo: Optional[str] = None
    # Финальное саммари агента (что сделано) — из его последнего отчёта.
    # Пишется отдельным сообщением перед коротким уведомлением со ссылками.
    agent_summary: Optional[str] = None

    def add_log(self, message: str) -> None:
        self.log.append(f"[{datetime.utcnow().isoformat()}] {message}")
