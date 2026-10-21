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

    def get_git_credentials(self) -> tuple[str, str]:
        """Логин и токен для git push.

        GitHub App нельзя аутентифицировать в git по логину/паролю, поэтому
        используем installation token как пароль. Токен у PyGithub запрашивается
        лениво, поэтому сначала прикрепляем requester через _ensure_auth().
        """
        self._ensure_auth()
        assert self._gh is not None
        # .token нельзя трогать до прикрепления requester — поэтому идём
        # через авторизацию клиента, которая уже использована в запросах.
        auth = self._gh.requester.auth
        token = getattr(auth, "token", None)
        if not token:
            raise RuntimeError("не удалось получить installation token")
        return "x-access-token", token

    def repo_clone_url(self) -> str:
        """URL репозитория для git clone/push."""
        return f"https://github.com/{self.repo_full_name}.git"

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

    def create_comment(self, issue_number: int, body: str) -> int:
        """Оставить комментарий к issue. Возвращает id комментария."""
        self._ensure_auth()
        assert self._repo is not None
        issue = self._repo.get_issue(number=issue_number)
        comment = issue.create_comment(body)
        return comment.id

    def update_comment(self, comment_id: int, body: str) -> None:
        """Отредактировать существующий комментарий (например, зафиксировать ответ)."""
        self._ensure_auth()
        assert self._repo is not None
        comment = self._repo.get_issue_comment(comment_id)
        comment.edit(body)

    def fetch_issue_comments(
        self, issue_number: int, after_comment_id: Optional[int] = None
    ) -> list[dict]:
        """Комментарии issue живых участников (без ботов).

        after_comment_id — вернуть только комментарии, созданные после
        комментария с этим id (например, после нашего вопроса автору).
        """
        self._ensure_auth()
        assert self._repo is not None
        issue = self._repo.get_issue(number=issue_number)
        result = []
        for comment in issue.get_comments():
            if after_comment_id is not None and comment.id <= after_comment_id:
                continue
            user = comment.user
            if user is not None and getattr(user, "type", None) == "Bot":
                continue
            result.append(
                {
                    "id": comment.id,
                    "author": user.login if user else None,
                    "body": comment.body or "",
                }
            )
        return result

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
