"""GitHub Adapter: GitHub App аутентификация, issues, комментарии, PR.

Использует GitHub App (JWT -> installation token) вместо PAT.
"""

from __future__ import annotations

import time
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
        # Прямой PATCH /repos/{owner}/{repo}/issues/comments/{id}:
        # Repository.get_comment в PyGithub указывает на комментарии
        # к коммитам (другой эндпоинт и другие права).
        self._repo.requester.requestJsonAndCheck(
            "PATCH",
            f"{self._repo.url}/issues/comments/{comment_id}",
            input={"body": body},
        )

    def _gh_api(self, method: str, path: str, input: Optional[dict] = None) -> dict:
        """Вызов GitHub API от имени установки, возвращает JSON-ответ."""
        self._ensure_auth()
        assert self._repo is not None
        kw = {"input": input} if input is not None else {}
        _h, data = self._repo.requester.requestJsonAndCheck(
            method, f"{self._repo.url}{path}", **kw
        )
        return data if isinstance(data, dict) else {}

    def find_workflow_run(
        self, head_sha: str, timeout_s: int = 40, poll_every_s: float = 8.0
    ) -> Optional[dict]:
        """Найти запуск GitHub Actions для коммита.

        Триггер workflow срабатывает с лагом в несколько секунд после push,
        поэтому поллим до timeout_s. Блокирующий — вызывать через
        asyncio.to_thread. None — запуск так и не появился.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            data = self._gh_api(
                "GET", f"/actions/runs?head_sha={head_sha}&per_page=10"
            )
            runs = data.get("workflow_runs") or []
            if runs:
                r = runs[0]
                return {
                    "id": r["id"],
                    "html_url": r["html_url"],
                    "status": r["status"],
                    "conclusion": r.get("conclusion"),
                    "name": r.get("name"),
                }
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_every_s)

    def get_workflow_run(self, run_id: int) -> Optional[dict]:
        """Статус запуска GitHub Actions (status/conclusion/html_url)."""
        data = self._gh_api("GET", f"/actions/runs/{run_id}")
        return {
            "id": data.get("id"),
            "html_url": data.get("html_url"),
            "status": data.get("status"),
            "conclusion": data.get("conclusion"),
        }

    def list_run_artifacts(self, run_id: int) -> list[dict]:
        """Артефакты запуска: [{id, name, size_in_bytes}] (загруженные)."""
        data = self._gh_api(
            "GET", f"/actions/runs/{run_id}/artifacts?per_page=100"
        )
        return [
            {
                "id": a["id"],
                "name": a.get("name") or "",
                "size_in_bytes": a.get("size_in_bytes") or 0,
            }
            for a in (data.get("artifacts") or [])
            if not a.get("expired")
        ]

    def fetch_issue_state(self, issue_number: int) -> str:
        """Состояние issue: 'open' или 'closed'."""
        self._ensure_auth()
        assert self._repo is not None
        issue = self._repo.get_issue(number=issue_number)
        return issue.state

    def last_bot_comment_id(self, issue_number: int) -> Optional[int]:
        """ID последнего комментария бота к issue (наши вопросы и уведомления).

        Нужен при восстановлении уточнений после рестарта: ответом считаем
        только комментарии, оставленные ПОСЛЕ нашего последнего комментария.
        None — у issue ещё нет комментариев от бота.
        """
        self._ensure_auth()
        assert self._repo is not None
        issue = self._repo.get_issue(number=issue_number)
        last_id: Optional[int] = None
        for comment in issue.get_comments():
            user = comment.user
            if user is not None and getattr(user, "type", None) == "Bot":
                last_id = comment.id
        return last_id

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
