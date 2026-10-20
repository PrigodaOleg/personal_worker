"""Сборка и тесты для iOS — делегирование на iMac по SSH."""

from __future__ import annotations

import os

from orchestrator.adapters.imac import IMacAdapter


class IOSRunner:
    def __init__(self, imac: IMacAdapter) -> None:
        self.imac = imac

    async def build_ios(self, workspace: str) -> dict:
        project_dir = os.path.join(workspace, "domains/client")
        return await self.imac.run_ios_build(project_dir)
