"""iMac adapter: сборка и тесты iOS по SSH (заглушка)."""

from __future__ import annotations


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
        self.ssh_key = ssh_key
        self.work_dir = work_dir

    async def run_ios_build_and_test(self, project_dir: str) -> dict:
        """Перенести проект на iMac, запустить flutter build ios + тесты."""
        print(f"[imac stub] {self.user}@{self.host}: build+test {project_dir}")
        return {"status": "stub"}
