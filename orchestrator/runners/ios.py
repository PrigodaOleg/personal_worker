"""Сборка и тесты для iOS — делегирование на iMac (заглушка)."""

from orchestrator.adapters.imac import IMacAdapter


class IOSRunner:
    def __init__(self, imac: IMacAdapter) -> None:
        self.imac = imac

    async def build_and_test(self, work_dir: str) -> bool:
        result = await self.imac.run_ios_build_and_test(work_dir)
        return result.get("status") == "ok"
