"""Общая логика запуска flutter-команд в Docker-контейнере flutter-builder."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class FlutterBaseRunner:
    def __init__(
        self,
        container_name: str = "flutter-builder",
        project_subpath: str = "domains/client",
        work_dir: str = "/mnt/ssd/personal_worker/workspaces",
    ) -> None:
        self.container_name = container_name
        self.project_subpath = project_subpath
        self.work_dir = work_dir

    def _project_dir(self, workspace: str) -> str:
        return os.path.join(workspace, self.project_subpath)

    async def _docker_exec(
        self, workspace: str, cmd: list[str]
    ) -> dict:
        full_cmd = [
            "docker", "exec", "-w",
            f"/workspace/{self.project_subpath}",
            self.container_name,
        ] + cmd
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    async def run_tests(self, workspace: str) -> dict:
        project_dir = self._project_dir(workspace)
        logger.info("[flutter] flutter test in %s", project_dir)
        res = await self._docker_exec(workspace, ["flutter", "test"])
        return res

    async def build_linux(self, workspace: str) -> dict:
        logger.info("[flutter] flutter build linux in %s", self._project_dir(workspace))
        return await self._docker_exec(workspace, ["flutter", "build", "linux"])

    async def build_web(self, workspace: str) -> dict:
        logger.info("[flutter] flutter build web in %s", self._project_dir(workspace))
        return await self._docker_exec(workspace, ["flutter", "build", "web", "--release"])

    async def build_android(self, workspace: str) -> dict:
        logger.info("[flutter] flutter build apk in %s", self._project_dir(workspace))
        return await self._docker_exec(
            workspace, ["flutter", "build", "apk", "--release", "--split-per-abi"]
        )

    async def build_ios(self, workspace: str) -> dict:
        """iOS не собирается здесь — делегируется на iMac."""
        raise NotImplementedError("iOS build is handled by IMacAdapter")
