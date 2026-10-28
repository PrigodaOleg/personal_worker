"""GitHub Adapter: GitHub App аутентификация, issues, комментарии, PR.

Использует GitHub App (JWT -> installation token) вместо PAT.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests
from github import Github
from github.Auth import AppAuth

from orchestrator.core.task import Task, TaskStatus

logger = logging.getLogger("personal_worker")


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

    # ------------------------------------------------------------------ #
    # Несколько репозиториев
    # ------------------------------------------------------------------ #
    def for_repo(self, repo_full_name: str) -> "GitHubAdapter":
        """Клон адаптера, привязанный к другому репозиторию.

        Один GitHub App может обслуживать несколько репозиториев и даже
        несколько installation (по одной на аккаунт). Клиент _gh (токен
        установки) переиспользуется, если он уже создан для того же
        installation_id; иначе пересоздаётся лениво.
        """
        clone = GitHubAdapter(
            app_id=self.app_id,
            private_key_path=self.private_key_path,
            installation_id=self.installation_id,
            repo_full_name=repo_full_name,
            label=self.label,
        )
        if self._gh is not None:
            # Тот же installation_id → токен и клиент валидны, переиспользуем.
            clone._gh = self._gh
        return clone

    def with_installation(self, installation_id: int) -> "GitHubAdapter":
        """Клон адаптера под другой installation (другой аккаунт-владелец)."""
        clone = self.for_repo(self.repo_full_name)
        clone.installation_id = installation_id
        return clone

    def has_repo_access(self, repo_full_name: str) -> bool:
        """Есть ли у установки доступ к репозиторию (без исключений)."""
        try:
            self._repo_obj(repo_full_name)
            return True
        except Exception:  # noqa: BLE001 — 404/403 значит «нет доступа»
            return False

    def _repo_obj(self, repo_full_name: str):
        """Объект репозитория (проверка доступа). Кэш не нужен: вызовы редки."""
        self._ensure_auth()
        assert self._gh is not None
        return self._gh.get_repo(repo_full_name)

    def ensure_label(self, repo_full_name: Optional[str] = None) -> bool:
        """Создать метку задач в репозитории, если её там нет.

        True — метка есть (создана или уже существовала), False — не
        удалось (нет доступа). Цвет дефолтный GitHub-серый; имя берём
        из self.label.
        """
        name = repo_full_name or self.repo_full_name
        try:
            repo = self._repo_obj(name)
            try:
                repo.get_label(self.label)
                return True
            except Exception:  # noqa: BLE001 — метки нет, создаём
                repo.create_label(name=self.label, color="6e7781", description="Задача для personal_worker")
                return True
        except Exception:  # noqa: BLE001
            logger.warning("[github] не удалось создать метку в %s", name, exc_info=True)
            return False

    def app_slug(self) -> Optional[str]:
        """Слаг приложения (для ссылок-инструкций). Нужен app-level JWT."""
        try:
            import jwt as pyjwt

            private_key = self._load_private_key()
            now = int(time.time())
            payload = {"iat": now - 60, "exp": now + 600, "iss": str(self.app_id)}
            token = pyjwt.encode(payload, private_key, algorithm="RS256")
            import requests as _requests

            r = _requests.get(
                "https://api.github.com/app",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
            )
            if r.status_code == 200:
                return r.json().get("slug")
        except Exception:  # noqa: BLE001 — slug не критичен
            logger.warning("[github] не удалось получить app slug", exc_info=True)
        return None

    def list_installations(self) -> list[dict]:
        """Все установки приложения: [{id, account_login, repo_selection}]."""
        try:
            import jwt as pyjwt
            import requests as _requests

            private_key = self._load_private_key()
            now = int(time.time())
            payload = {"iat": now - 60, "exp": now + 600, "iss": str(self.app_id)}
            token = pyjwt.encode(payload, private_key, algorithm="RS256")
            r = _requests.get(
                "https://api.github.com/app/installations",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
            )
            if r.status_code != 200:
                return []
            return [
                {
                    "id": i["id"],
                    "account_login": (i.get("account") or {}).get("login"),
                    "repo_selection": i.get("repository_selection"),
                }
                for i in r.json()
            ]
        except Exception:  # noqa: BLE001
            logger.warning("[github] не удалось получить список installation", exc_info=True)
            return []

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
        return data or None

    def list_run_jobs(self, run_id: int) -> list[dict]:
        """Джобы запуска GHA: id, имя, conclusion (для разбора падений)."""
        data = self._gh_api("GET", f"/actions/runs/{run_id}/jobs?per_page=30")
        out = []
        for j in data.get("jobs") or []:
            out.append({
                "id": j.get("id"),
                "name": j.get("name"),
                "conclusion": j.get("conclusion"),
                "html_url": j.get("html_url"),
            })
        return out

    _FAILED_JOB_CONCLUSIONS = ("failure", "cancelled", "timed_out")

    def get_job_logs(self, job_id: int) -> Optional[str]:
        """Полный лог джобы (plain text). Логи GHA отдаются редиректом
        на подписанный URL — нужен запрос с allow_redirects."""
        self._ensure_auth()
        assert self._repo is not None
        token = self._repo.requester.auth.token
        try:
            r = requests.get(
                f"https://api.github.com/repos/{self._repo.full_name}"
                f"/actions/jobs/{job_id}/logs",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                allow_redirects=True,
                timeout=60,
            )
            if r.status_code != 200:
                return None
            return r.text
        except requests.RequestException:
            return None

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

    def get_issue_assignees(self, issue_number: int) -> list[str]:
        """Логины assignees issue — кандидаты в ревьюеры PR и упоминания."""
        data = self._gh_api("GET", f"/issues/{issue_number}")
        logins = [
            (a or {}).get("login") for a in (data.get("assignees") or [])
        ]
        return [login for login in logins if login]

    def request_reviewers(self, pr_url: str, reviewers: list[str]) -> None:
        """Запросить ревью у пользователей на созданный PR.

        Принимает html_url PR (число достаётся из /pull/{n}). Ошибка 422
        (например, пользователь без доступа к репозиторию) летит наверх —
        вызывающий код логирует её, но не валит задачу: PR уже создан.
        """
        self._ensure_auth()
        assert self._repo is not None
        m = re.search(r"/pull/(\d+)", pr_url)
        if not m:
            raise ValueError(f"не похоже на URL PR: {pr_url}")
        self._gh_api(
            "POST",
            f"/pulls/{m.group(1)}/requested_reviewers",
            input={"reviewers": list(reviewers)},
        )

    def fetch_pr_head_branch(self, pr_url: str) -> Optional[str]:
        """Имя head-ветки PR (для допуша исправлений)."""
        self._ensure_auth()
        assert self._repo is not None
        m = re.search(r"/pull/(\d+)", pr_url)
        if not m:
            return None
        try:
            pr = self._repo.get_pull(number=int(m.group(1)))
            return pr.head.ref
        except Exception:  # noqa: BLE001 — PR могли удалить
            return None

    def get_pr_merge_info(self, pr_url: str) -> Optional[dict]:
        """Состояние мержа PR: mergeable, mergeable_state, base-ветка.

        mergeable_state:
          dirty   — merge-конфликт с base-веткой, нужен резолв;
          behind  — ветка отстала от base (мержится, но надо обновить);
          blocked — ждём обязательные проверки;
          clean/unstable — мерж доступен.
        None — PR недоступен или GitHub ещё считает (mergeable is None).
        """
        self._ensure_auth()
        assert self._repo is not None
        m = re.search(r"/pull/(\d+)", pr_url)
        if not m:
            return None
        try:
            pr = self._repo.get_pull(number=int(m.group(1)))
            if pr.state == "closed" or pr.merged:
                return {"state": pr.state, "merged": bool(pr.merged)}
            if pr.mergeable is None:  # GitHub ещё пересчитывает
                return None
            return {
                "state": pr.state,
                "merged": False,
                "mergeable": bool(pr.mergeable),
                "mergeable_state": pr.mergeable_state,
                "base_ref": pr.base.ref,
                "head_ref": pr.head.ref,
                "head_sha": pr.head.sha,
                "number": int(m.group(1)),
            }
        except Exception:  # noqa: BLE001 — PR могли удалить
            return None

    def fetch_pr_feedback(
        self, pr_url: str, after_comment_id: int = 0
    ) -> list[dict]:
        """Новые отзывы людей по PR: issue-комментарии + ревью + инлайн-комментарии.

        Возвращает [{id, author, body, kind}] в хронологическом порядке:
        kind = issue_comment | review | inline. Боты (включая наш
        финальный комментарий) отфильтрованы. after_comment_id — вернуть
        только комментарии с id больше него.
        """
        self._ensure_auth()
        assert self._repo is not None
        m = re.search(r"/pull/(\d+)", pr_url)
        if not m:
            return []
        pr_number = int(m.group(1))
        out: list[dict] = []

        def _bot(u) -> bool:
            return bool(u and getattr(u, "type", None) == "Bot")

        # 1) Обычные комментарии PR (issue-комментарии).
        data = self._gh_api(
            "GET", f"/issues/{pr_number}/comments?per_page=50&sort=created&direction=asc"
        )
        for c in data or []:
            if c.get("id", 0) <= after_comment_id or _bot(c.get("user")):
                continue
            out.append({
                "id": c["id"],
                "author": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "kind": "issue_comment",
            })

        # 2) Ревью с телом (approve/request changes/comments).
        data = self._gh_api(
            "GET", f"/pulls/{pr_number}/reviews?per_page=50"
        )
        for r in data or []:
            if r.get("id", 0) <= after_comment_id or _bot(r.get("user")):
                continue
            if not (r.get("body") or "").strip():
                continue
            out.append({
                "id": r["id"],
                "author": (r.get("user") or {}).get("login"),
                "body": r.get("body") or "",
                "kind": f"review:{r.get('state', '').lower()}",
            })

        # 3) Инлайн-комментарии к строкам кода.
        data = self._gh_api(
            "GET", f"/pulls/{pr_number}/comments?per_page=50&sort=created&direction=asc"
        )
        for c in data or []:
            if c.get("id", 0) <= after_comment_id or _bot(c.get("user")):
                continue
            out.append({
                "id": c["id"],
                "author": (c.get("user") or {}).get("login"),
                "body": (c.get("body") or "").strip(),
                "kind": "inline",
                "path": c.get("path"),
            })
        out.sort(key=lambda x: x["id"])
        return out

    def task_to_status(self, task: Task) -> TaskStatus:
        """Вспомогательная маппинг (для будущего использования)."""
        return task.status
