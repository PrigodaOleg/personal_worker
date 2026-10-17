"""Общая логика сборок и тестов Flutter (заглушка)."""

from __future__ import annotations


class FlutterBaseRunner:
    def __init__(self, sdk_path: str | None = None) -> None:
        self.sdk_path = sdk_path

    async def run_tests(self, work_dir: str) -> bool:
        print(f"[flutter_base stub] flutter test in {work_dir}")
        return True

    async def build(self, target: str, work_dir: str) -> bool:
        print(f"[flutter_base stub] flutter build {target} in {work_dir}")
        return True
