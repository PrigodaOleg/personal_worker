"""GitHub adapter: чтение issues, комментарии, создание PR (заглушка)."""

from __future__ import annotations

import os

from github import Github


class GitHubAdapter:
    def __init__(self, token: str, repo_full_name: str) -> None:
        self._gh = Github(token)
        self._repo = self._gh.get_repo(repo_full_name)

    async def fetch_open_issues(self) -> list[dict]:
        """Вернуть открытые issues, помеченные для обработки."""
        # TODO: фильтр по label, например "personal-worker"
        issues = self._repo.get_issues(state="open")
        return [
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body or "",
                "url": issue.html_url,
            }
            for issue in issues
        ]

    async def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        """Создать PR и вернуть URL."""
        pr = self._repo.create_pull(title=title, body=body, base=base, head=branch)
        return pr.html_url
