"""iMac Adapter: сборка и тесты iOS по SSH.

Требует настройки SSH-ключа (без пароля) от этого ПК к iMac.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class IMacAdapter:
    def __init__(
        self,
        host: str,
        user: str,
        ssh_key: str = "~/.ssh/id_ed25519",
        work_dir: str = "~/personal_worker",
    ) -> None:
        self.host = host
        self.user = user
        self.ssh_key = os.path.expanduser(ssh_key)
        self.work_dir = work_dir

    def _ssh_cmd(self, remote_cmd: str) -> str:
        return (
            f"ssh -i {self.ssh_key} -o StrictHostKeyChecking=accept-new "
            f"{self.user}@{self.host} '{remote_cmd}'"
        )

    def _scp_cmd(self, local_path: str, remote_path: str) -> str:
        return (
            f"scp -i {self.ssh_key} -o StrictHostKeyChecking=accept-new -r "
            f"{local_path} {self.user}@{self.host}:{remote_path}"
        )

    async def run_ios_build_and_test(self, project_dir: str) -> dict:
        """Перенести проект на iMac, запустить flutter build ios + тесты."""
        logger.info("[imac] preparing remote work dir")
        mkdir = await self._run(f"mkdir -p {self.work_dir}")
        if mkdir["returncode"] != 0:
            return {"status": "error", "error": mkdir["stderr"]}

        logger.info("[imac] copying project to iMac")
        copy = await self._run(self._scp_cmd(project_dir, self.work_dir))
        if copy["returncode"] != 0:
            return {"status": "error", "error": copy["stderr"]}

        project_name = os.path.basename(project_dir.rstrip("/"))
        remote_project = f"{self.work_dir}/{project_name}"

        logger.info("[imac] flutter test on iMac")
        test_cmd = f"cd {remote_project} && flutter test"
        test = await self._run(self._ssh_cmd(test_cmd))
        if test["returncode"] != 0:
            return {
                "status": "test_failed",
                "error": test["stderr"],
                "stdout": test["stdout"],
            }

        logger.info("[imac] flutter build ios on iMac")
        build_cmd = f"cd {remote_project} && flutter build ios --release"
        build = await self._run(self._ssh_cmd(build_cmd))
        if build["returncode"] != 0:
            return {
                "status": "build_failed",
                "error": build["stderr"],
                "stdout": build["stdout"],
            }

        return {
            "status": "ok",
            "stdout": build["stdout"],
            "test_stdout": test["stdout"],
        }

    async def _run(self, cmd: str) -> dict:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
