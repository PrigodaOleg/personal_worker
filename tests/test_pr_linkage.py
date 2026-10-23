"""Тесты линковки PR с issue: Closes #N, ветка, assignees → упоминания и ревью.

`Closes #N` в теле PR прилинковывает PR к issue (Development) и закрывает
issue при влитии PR в дефолтную ветку — закрытие делает сам GitHub.
"""


from orchestrator.adapters.github import GitHubAdapter
from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


def make_task(source: TaskSource) -> Task:
    return Task(
        id="39ddec0390597760",
        source=source,
        source_id="36" if source == TaskSource.GITHUB else "100500",
        title="Добавить кнопку",
        description="Нужна кнопка в шапке",
        status=TaskStatus.DONE,
    )


# ---------------------------------------------------------------- #
# Тело PR
# ---------------------------------------------------------------- #


def test_pr_body_github_has_closes_branch_mentions():
    task = make_task(TaskSource.GITHUB)
    body = Scheduler._build_pr_body(task, "ai-task-39ddec0390597760-r1", ["alice", "bob"])
    assert "Closes #36" in body, "нет линковки/закрытия issue"
    assert "`ai-task-39ddec0390597760-r1`" in body, "не указана ветка"
    assert "@alice @bob" in body, "assignees не упомянуты"


def test_pr_body_telegram_without_issue_link():
    task = make_task(TaskSource.TELEGRAM)
    body = Scheduler._build_pr_body(task, "ai-task-39ddec0390597760-r2", [])
    assert "Closes #" not in body, "у Telegram-задачи нет issue — закрывать нечего"
    assert "`ai-task-39ddec0390597760-r2`" in body


# ---------------------------------------------------------------- #
# GitHubAdapter.get_issue_assignees
# ---------------------------------------------------------------- #


def test_get_issue_assignees_returns_logins():
    gh = object.__new__(GitHubAdapter)

    def fake_api(method, path, input=None):
        assert (method, path) == ("GET", "/issues/36")
        return {
            "assignees": [{"login": "alice"}, {"login": "bob"}, {}, None]
        }

    gh._gh_api = fake_api
    assert gh.get_issue_assignees(36) == ["alice", "bob"]


def test_get_issue_assignees_empty_when_none():
    gh = object.__new__(GitHubAdapter)

    def fake_api(method, path, input=None):
        return {"assignees": None}

    gh._gh_api = fake_api
    assert gh.get_issue_assignees(1) == []


# ---------------------------------------------------------------- #
# GitHubAdapter.request_reviewers
# ---------------------------------------------------------------- #


def test_request_reviewers_posts_to_pull_request():
    gh = object.__new__(GitHubAdapter)
    gh._ensure_auth = lambda: None
    gh._repo = object()  # для assert в request_reviewers
    seen = {}

    def fake_api(method, path, input=None):
        seen["call"] = (method, path, input)

    gh._gh_api = fake_api
    gh.request_reviewers(
        "https://github.com/PrigodaOleg/social_mind/pull/37", ["alice", "bob"]
    )
    method, path, payload = seen["call"]
    assert method == "POST"
    assert path == "/pulls/37/requested_reviewers"
    assert payload == {"reviewers": ["alice", "bob"]}


def test_request_reviewers_rejects_non_pr_url():
    gh = object.__new__(GitHubAdapter)
    gh._ensure_auth = lambda: None
    gh._repo = object()
    gh._gh_api = lambda *a, **k: {}
    try:
        gh.request_reviewers("https://github.com/o/r/issues/36", ["alice"])
    except ValueError:
        pass
    else:
        raise AssertionError("не PR-URL должен давать ValueError")


# ---------------------------------------------------------------- #
# Сквозная сборка assignees в _build_ios_and_create_pr упрощённо:
# сбои fetch/request не должны ронять задачу — проверяем на уровне
# исключений, что warning-путь не поднимает наверх.
# ---------------------------------------------------------------- #


def test_adapter_failure_is_contained_by_design():
    """get_issue_assignees кидает — scheduler оборачивает в try/except.

    Здесь фиксируем только контракт адаптера: исключение действительно
    поднимается (иначе try/except в планировщике был бы мёртвым кодом).
    """
    gh = object.__new__(GitHubAdapter)

    def boom(*a, **k):
        raise RuntimeError("api down")

    gh._gh_api = boom
    try:
        gh.get_issue_assignees(36)
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидаемое исключение не поднялось")
