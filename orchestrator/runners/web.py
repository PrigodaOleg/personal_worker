"""Сборка и тесты для Web (в контейнере flutter-builder)."""

from __future__ import annotations

from orchestrator.runners.flutter_base import FlutterBaseRunner


class WebRunner(FlutterBaseRunner):
    async def build_and_test(self, workspace: str) -> dict:
        test = await self.run_tests(workspace)
        if test["returncode"] != 0:
            return {"status": "test_failed", **test}
        build = await self.build_web(workspace)
        if build["returncode"] != 0:
            return {"status": "build_failed", **build}
        return {"status": "ok", "test_stdout": test["stdout"], "build_stdout": build["stdout"]}
