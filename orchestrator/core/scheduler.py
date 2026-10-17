"""Планировщик задач: приём, уточнения, запуск OpenHands."""

from __future__ import annotations

from typing import Iterable

from orchestrator.core.state import StateStore
from orchestrator.core.task import Task, TaskSource, TaskStatus


class Scheduler:
    def __init__(self, sources: Iterable[str], poll_interval: int = 30) -> None:
        self.sources = list(sources)
        self.poll_interval = poll_interval
        self.state = StateStore("state/orchestrator.sqlite")

    async def run(self) -> None:
        print(f"[scheduler] sources={self.sources} interval={self.poll_interval}s")
        # TODO: подключить адаптеры GitHub, Telegram, OpenHands после ответов на вопросы
        while True:
            print("[scheduler] tick (stub)")
