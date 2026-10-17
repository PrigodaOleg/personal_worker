"""Сборка и тесты для Web (заглушка)."""

from orchestrator.runners.flutter_base import FlutterBaseRunner


class WebRunner(FlutterBaseRunner):
    async def build(self, work_dir: str) -> bool:
        print("[web stub] flutter build web")
        return True
