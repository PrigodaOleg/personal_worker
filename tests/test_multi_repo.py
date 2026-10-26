"""Мульти-репозиторий: настройки, опрос, Telegram-команды, доступ App.

Тесты без сети: Scheduler через object.__new__, GitHubAdapter — фейки.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import TaskSource

DEFAULT_REPO = "PrigodaOleg/social_mind"
OTHER_REPO = "PrigodaOleg/other_app"


class FakeState:
    def __init__(self):
        self.settings: dict[str, str] = {}
        self.tasks: dict[str, object] = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def get(self, task_id):
        return self.tasks.get(task_id)

    def save(self, task):
        self.tasks[task.id] = task

    def list_by_status(self, status):
        return [
            t for t in self.tasks.values() if t.status == status
        ]


class FakeAdapter:
    """Фейк GitHubAdapter для github_for / poll / repo-команд."""

    labels_created: list[str] = []  # общий журнал: клоны адаптера делят его

    def __init__(self, accessible: set[str], label: str = "aitask",
                 repo_full_name: str | None = None):
        self.accessible = accessible
        self.label = label
        self.repo_full_name = repo_full_name
        self.app_slug_value: str | None = "test-app"

    def for_repo(self, repo_full_name):
        return FakeAdapter(self.accessible, self.label, repo_full_name)

    def with_installation(self, installation_id):
        return self

    def has_repo_access(self, repo_full_name):
        return repo_full_name in self.accessible

    def fetch_open_issues(self):
        # Адаптер привязан к конкретному репо — возвращает только его issues.
        repo = self.repo_full_name
        if repo is None or repo not in self.accessible:
            return []
        return [
            {
                "number": 1,
                "title": f"issue in {repo}",
                "body": "",
                "url": f"https://github.com/{repo}/issues/1",
                "author": "someone",
            }
        ]

    def ensure_label(self, repo_full_name=None):
        self.labels_created.append(repo_full_name)
        return True

    def app_slug(self):
        return self.app_slug_value

    def list_installations(self):
        return []


def make_scheduler(issues_map: dict[str, bool], settings: dict | None = None):
    s = object.__new__(Scheduler)
    s._pending_notifications = set()
    s._background_tasks = set()
    s._repo_adapters = {}
    s.state = FakeState()
    if settings:
        s.state.settings.update(settings)
    s.config = types.SimpleNamespace(
        github_repo=DEFAULT_REPO,
        github_label="aitask",
        telegram_admin_username="boss",
    )
    s.github = FakeAdapter(set(issues_map), repo_full_name=DEFAULT_REPO)
    sent: list[tuple[str, str]] = []
    async def _send(chat_id, text, keyboard=None):
        sent.append((chat_id, text))

    FakeAdapter.labels_created = []
    s.telegram = types.SimpleNamespace(send_message=_send)
    # Мапа репо → issues; fetch_open_issues у нас упрощённый, поэтому для
    # полла подменяем адаптеры на фейки с нужным репозиторием.
    s._enqueue_task = lambda task: None
    s._notify_author = lambda task, text: None
    s._notify_admin = lambda text: sent.append(("admin", text))
    s._cancel_tasks_for_closed_issues = lambda: None
    s._poll_github_clarification_answers = lambda: None
    s._poll_github_agent_question_answers = lambda: None
    return s, sent


class TestConfiguredRepos:
    def test_default_when_unset(self):
        s, _ = make_scheduler({})
        assert s.configured_repos() == [DEFAULT_REPO]

    def test_roundtrip_and_default_kept(self):
        s, _ = make_scheduler({})
        s.set_configured_repos(["a/b"])
        assert s.configured_repos() == ["a/b", DEFAULT_REPO]
        s.set_configured_repos(["a/b", DEFAULT_REPO])
        assert s.configured_repos() == ["a/b", DEFAULT_REPO]

    def test_corrupt_json_falls_back(self):
        s, _ = make_scheduler({}, settings={"repos": "not json"})
        assert s.configured_repos() == [DEFAULT_REPO]


class TestTaskIdScopes:
    def test_non_default_repo_prefixed_id(self):
        s, _ = make_scheduler({})
        legacy = s._task_id(TaskSource.GITHUB, "41")
        other = s._task_id(TaskSource.GITHUB, f"{OTHER_REPO}:41")
        assert legacy != other  # номера issue не конфликтуют между репо

    def test_poll_creates_tasks_with_repo(self):
        # OTHER_REPO добавлен в опрос → poll создаёт по задаче на репо.
        s, sent = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        s.set_configured_repos([OTHER_REPO])
        asyncio.run(s.poll_github())
        repos = {t.repo for t in s.state.tasks.values()}
        assert repos == {DEFAULT_REPO, OTHER_REPO}
        # Дефолтное репо сохранило legacy-id, другое — префиксованное.
        legacy = s._task_id(TaskSource.GITHUB, "1")
        prefixed = s._task_id(TaskSource.GITHUB, f"{OTHER_REPO}:1")
        assert legacy in s.state.tasks and prefixed in s.state.tasks

    def test_poll_no_duplicate_between_polls(self):
        s, _ = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        s.set_configured_repos([OTHER_REPO])
        asyncio.run(s.poll_github())
        n = len(s.state.tasks)
        asyncio.run(s.poll_github())
        assert len(s.state.tasks) == n


class TestGithubFor:
    def test_default_repo_returns_main_adapter(self):
        s, _ = make_scheduler({})
        assert s.github_for(None) is s.github
        assert s.github_for(DEFAULT_REPO) is s.github

    def test_caches_clone(self):
        s, _ = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        a = s.github_for(OTHER_REPO)
        b = s.github_for(OTHER_REPO)
        assert a is b
        assert a is not s.github

    def test_no_access_returns_clone_anyway(self):
        s, _ = make_scheduler({DEFAULT_REPO})  # OTHER_REPO недоступен
        a = s.github_for("unknown/repo")
        assert a.repo_full_name == "unknown/repo"


class TestRepoCommands:
    def test_non_admin_rejected(self):
        s, sent = make_scheduler({})
        asyncio.run(
            s._handle_repo_command(1, "/repo_add", ["/repo_add", OTHER_REPO], "hacker")
        )
        assert any("админу" in t for _, t in sent)
        assert s.configured_repos() == [DEFAULT_REPO]

    def test_bad_name_rejected(self):
        s, sent = make_scheduler({})
        asyncio.run(
            s._handle_repo_command(1, "/repo_add", ["/repo_add", "no-slash"], "boss")
        )
        assert any("Формат" in t for _, t in sent)

    def test_add_without_access_gives_instructions(self):
        s, sent = make_scheduler({DEFAULT_REPO})
        asyncio.run(
            s._handle_repo_command(1, "/repo_add", ["/repo_add", OTHER_REPO], "boss")
        )
        text = sent[-1][1]
        assert "нет доступа" in text
        assert "github.com/apps/test-app/installations/new" in text
        assert f"/repo check {OTHER_REPO}" in text
        assert s.configured_repos() == [DEFAULT_REPO]  # не добавлен

    def test_add_with_access_saves_and_creates_label(self):
        s, sent = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        asyncio.run(
            s._handle_repo_command(1, "/repo_add", ["/repo_add", OTHER_REPO], "boss")
        )
        text = sent[-1][1]
        assert "подключен" in text
        assert s.configured_repos() == [OTHER_REPO, DEFAULT_REPO]
        assert s.github.labels_created == [OTHER_REPO]

    def test_check_keeps_settings(self):
        s, _ = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        s.set_configured_repos([OTHER_REPO])
        asyncio.run(
            s._handle_repo_command(1, "/repo_check", ["/repo_check", OTHER_REPO], "boss")
        )
        assert s.configured_repos() == [OTHER_REPO, DEFAULT_REPO]
        assert len(s.github.labels_created) == 1

    def test_del_removes(self):
        s, sent = make_scheduler({DEFAULT_REPO, OTHER_REPO})
        s.set_configured_repos([OTHER_REPO])
        asyncio.run(
            s._handle_repo_command(1, "/repo_del", ["/repo_del", OTHER_REPO], "boss")
        )
        assert s.configured_repos() == [DEFAULT_REPO]
        assert any("убран" in t for _, t in sent)

    def test_del_default_repo_blocked_implicitly(self):
        # Дефолтный репо нельзя убрать: set_configured_repos возвращает его.
        s, _ = make_scheduler({DEFAULT_REPO})
        asyncio.run(
            s._handle_repo_command(
                1, "/repo_del", ["/repo_del", DEFAULT_REPO], "boss"
            )
        )
        assert s.configured_repos() == [DEFAULT_REPO]

    def test_is_admin(self):
        s, _ = make_scheduler({})
        assert s._is_admin("boss")
        assert s._is_admin("@boss")
        assert not s._is_admin("someone")
        assert not s._is_admin(None)
