"""GitHub Adapter: GitHub App аутентификация, issues, комментарии, PR.

Использует GitHub App (JWT -> installation token) вместо PAT.
"""

from __future__ import annotations

from typing import Optional

from github import Github
from github.Auth import AppAuth

from orchestrator.core.task import Task, TaskSource, TaskStatus


class GitHubAdapter:
    def __init__(
        self,
        app_id: int,
        private_key_path: str,
        installation_id: int,
        repo_full_name: str,
        label: str,
    ) -> None:
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.label = label
        self._gh: Optional[Github] = None
        self._repo = None
        self._auth_token: Optional[str] = None
        self._token_expires_at: float = 0

    def _load_private_key(self) -> str:
        with open(self.private_key_path, "r") as f:
            return f.read()

    def _ensure_auth(self) -> None:
        """Создаёт GitHub клиент с GitHub App аутентификацией."""
        if self._gh is not None and self._repo is not None:
            return
        private_key = self._load_private_key()
        app_auth = AppAuth(app_id=self.app_id, private_key=private_key)
        installation_auth = app_auth.get_installation_auth(
            installation_id=self.installation_id
        )
        self._gh = Github(auth=installation_auth)
        self._repo = self._gh.get_repo(self.repo_full_name)

    def fetch_open_issues(self) -> list[dict]:
        """Вернуть открытые issues с меткой self.label."""
        self._ensure_auth()
        assert self._repo is not None
        issues = self._repo.get_issues(state="open", labels=[self.label])
        return [
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body or "",
                "url": issue.html_url,
                "author": issue.user.login if issue.user else None,
            }
            for issue in issues
        ]

    def create_comment(self, issue_number: int, body: str) -> None:
        """Оставить комментарий к issue."""
        self._ensure_auth()
        assert self._repo is not None
        issue = self._repo.get_issue(number=issue_number)
        issue.create_comment(body)

    def create_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> str:
        """Создать PR и вернуть URL."""
        self._ensure_auth()
        assert self._repo is not None
        pr = self._repo.create_pull(
            title=title, body=body, base=base, head=branch
        )
        return pr.html_url

    def task_to_status(self, task: Task) -> TaskStatus:
        """Вспомогательная маппинг (для будущего использования)."""
        return task.status
