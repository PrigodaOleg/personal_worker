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

    async def run_ios_build(self, project_dir: str) -> dict:
        """Перенести проект на iMac и собрать flutter build ios.

        Юнит-тесты iMac не гоняет: их уже валидирует runtime-контейнер
        OpenHands (та же Dart VM). К тому же на macOS VM-тесты падают на
        isar_plus (нативный символ не подгружается в тестовый процесс).
        Задача iMac-шага — собрать/слинковать iOS-бинарь.
        """
        logger.info("[imac] preparing remote work dir")
        project_name = os.path.basename(project_dir.rstrip("/"))
        remote_project = f"{self.work_dir}/{project_name}"
        # mkdir должен выполниться НА iMac (по SSH), а не на этой машине.
        # Заодно чистим прошлую копию: иначе scp -r положит проект внутрь
        # существующей папки (client/client) и сборка пойдёт по устаревшей копии.
        prep = await self._run(
            self._ssh_cmd(f"mkdir -p {self.work_dir} && rm -rf {remote_project}")
        )
        if prep["returncode"] != 0:
            return {"status": "error", "error": prep["stderr"]}

        logger.info("[imac] copying project to iMac")
        copy = await self._run(self._scp_cmd(project_dir, self.work_dir))
        if copy["returncode"] != 0:
            return {"status": "error", "error": copy["stderr"]}

        logger.info("[imac] flutter build ios on iMac")
        # --no-codesign: dev-сертификатов на iMac нет, а шаг нужен для валидации
        # компиляции/линковки iOS, а не для подписи и выкладки.
        build_cmd = f"cd {remote_project} && flutter build ios --release --no-codesign"
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
