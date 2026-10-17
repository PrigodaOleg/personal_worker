"""OpenHands adapter: запуск агента на разработку (заглушка)."""

from __future__ import annotations


class OpenHandsAdapter:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url
        self.api_key = api_key

    async def run_task(self, work_dir: str, instruction: str) -> dict:
        """Запустить OpenHands с инструкцией. Вернуть результат."""
        print(f"[openhands stub] run in {work_dir}: {instruction[:80]}...")
        return {"status": "stub", "pr_url": None}
