"""Сборка и тесты для Linux (заглушка)."""

from orchestrator.runners.flutter_base import FlutterBaseRunner


class LinuxRunner(FlutterBaseRunner):
    async def build(self, work_dir: str) -> bool:
        print("[linux stub] flutter build linux")
        return True
