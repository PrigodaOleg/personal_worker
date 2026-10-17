"""Сборка и тесты для Android (заглушка)."""

from orchestrator.runners.flutter_base import FlutterBaseRunner


class AndroidRunner(FlutterBaseRunner):
    async def build(self, work_dir: str) -> bool:
        print("[android stub] flutter build apk / appbundle")
        return True
