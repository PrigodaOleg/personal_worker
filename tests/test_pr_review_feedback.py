"""Ревью PR → агенту: комментарии/ревью/инлайн попадают в разговор.

Задача в DONE, но жизнь PR продолжается. Опрос (_poll_pr_review_feedback)
берёт новые отзывы живых людей (боты отфильтрованы в адаптере), трекает
«после какого id» в settings БД и отправляет агенту; после ответа агента
оркестратор синхронизирует правки и допушивает в ветку PR.
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.adapters.github import GitHubAdapter
from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeState:
    def __init__(self):
        self.settings: dict[str, str] = {}
        self.tasks: dict[str, Task] = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def save(self, task):
        self.tasks[task.id] = task

    def list_by_status(self, status):
        return [t for t in self.tasks.values() if t.status == status]


class FakeGitHub:
    def __init__(self, feedback=None, branch="ai-task-x-r1",
                 head_sha="abc1234", runs=None):
        self._feedback = feedback or []
        self._branch = branch
        self._head_sha = head_sha
        self._runs = runs or []
        self.feedback_calls: list[tuple[str, int]] = []

    def fetch_pr_feedback(self, pr_url, after_comment_id=0):
        self.feedback_calls.append((pr_url, after_comment_id))
        return self._feedback

    def fetch_pr_head_branch(self, pr_url):
        return self._branch

    def get_pr_head_sha(self, pr_url):
        return self._head_sha

    def list_workflow_runs(self, head_sha):
        return self._runs

    def last_bot_comment_id(self, issue_number):
        return 42


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text))


def make_scheduler(feedback=None, runs=None) -> Scheduler:
    s = object.__new__(Scheduler)
    s.state = FakeState()
    s.github = FakeGitHub(feedback, runs=runs)
    s.telegram = FakeTelegram()
    s.openhands = types.SimpleNamespace()
    s._repo_adapters = {}
    s._background_tasks = set()
    s._pending_notifications = set()
    s._notify_author_calls: list = []
    # Для тестов: ожидание готовности разговора выключено (переопределяется
    # в тестах, где оно проверяется).
    s.conversation_ready_timeout = 0
    s.conversation_ready_poll_s = 0.01
    # Для тестов: троттлинг опроса отключён.
    s.review_poll_min_interval = 0
    # Допуш в тестах не выполняем: правок в контейнере нет.
    async def no_push(*a, **kw):
        return None
    s._sync_and_push_ci_fix = no_push
    s.config = types.SimpleNamespace(
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id="999",
        work_dir="/tmp/pw-test-workspaces",
    )

    def notify_author(task, text):
        s._notify_author_calls.append(text)

    s._notify_author = notify_author
    s._spawn = lambda coro: None  # фоновые задачи не запускаем
    return s


def make_done_task() -> Task:
    t = Task(
        id="tk9",
        source=TaskSource.GITHUB,
        source_id="48",
        title="e2e тесты",
        description="",
        status=TaskStatus.DONE,
        conversation_id="conv9",
        repo="PrigodaOleg/social_mind",
    )
    t.pr_url = "https://github.com/PrigodaOleg/social_mind/pull/49"
    return t


def test_poll_forwards_feedback_to_agent():
    feedback = [
        {"id": 10, "author": "alice", "kind": "issue_comment",
         "body": "Пожалуйста, добавь тест на пустой ввод."},
        {"id": 11, "author": "bob", "kind": "inline", "path": "lib/x.dart",
         "body": "Лишний println."},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def capture(cid, message):
        s.captured = message
        return {"success": True}

    s.openhands.send_message = capture
    s._poll_pr_review_feedback()
    # _spawn заглушён — вызовем хендлер вручную.
    asyncio.run(s._handle_pr_feedback(task, feedback, 11))

    assert "ПУШИТЬ НЕ НУЖНО" in s.captured
    assert "добавь тест на пустой ввод" in s.captured


def test_feedback_state_tracked_in_settings():
    """Маркер «после какого id» продвигается только после доставки."""
    feedback = [
        {"id": 10, "author": "alice", "kind": "issue_comment", "body": "fix"},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)
    s.state.set_setting("pr_review_last_poll", "0")

    s._poll_pr_review_feedback()
    key = "pr_review_last_id:tk9"
    # Доставки ещё не было (_spawn заглушён) — маркер НЕ продвинут.
    assert s.state.get_setting(key) is None

    # Успешная доставка → маркер продвинут.
    async def ok(cid, message):
        return {"success": True}

    s.openhands.send_message = ok
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert s.state.get_setting(key) == "10"

    # Новых комментариев нет → после доставки feedback отдаётся пустой.
    s.github._feedback = []
    s._poll_pr_review_feedback()
    assert s.github.feedback_calls[-1] == (task.pr_url, 10)


def test_failed_delivery_not_consumed_and_retried():
    """Неудачная доставка не продвигает маркер — ретрай в след. опросе."""
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def fail(cid, message):
        return {"status": "error", "error": "boom"}

    async def not_ready(cid):
        return {"status": "error", "error": "timeout"}

    async def start_ok(cid):
        return {"status": "ok"}

    s.openhands.send_message = fail
    s.openhands.get_status = not_ready
    s.openhands.start_conversation = start_ok
    s._poll_pr_review_feedback()
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert s.state.get_setting("pr_review_last_id:tk9") is None
    assert s.state.get_setting("pr_review_attempts:tk9") == "1"
    assert s._notify_author_calls == []


def test_deleted_conversation_notifies_and_consumes():
    """Разговор удалён (404 на start) — уведомление, маркер продвинут."""
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def fail(cid, message):
        return {"status": "error", "error": "404 Not Found"}

    async def start_404(cid):
        return {"status": "error", "error": "404 Not Found"}

    s.openhands.send_message = fail
    s.openhands.start_conversation = start_404
    s._poll_pr_review_feedback()
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    # Маркер продвинут — вечных ретраев по мёртвому разговору не будет.
    assert s.state.get_setting("pr_review_last_id:tk9") == "10"
    assert len(s._notify_author_calls) == 1
    assert "удалён" in s._notify_author_calls[0]


def test_wakes_stopped_conversation_and_waits_for_ready():
    """Погасший разговор: start → ожидание STATUS$READY → повторная отправка."""
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)
    calls: list[str] = []

    async def send(cid, message):
        calls.append("send")
        # Первая попытка — разговор ещё спит; вторая — доставлено.
        return ({"status": "error", "error": "404"} if len(calls) == 1
                else {"success": True})

    statuses = iter([
        {"status": "STARTING", "runtime_status": "STATUS$STARTING"},
        {"status": "RUNNING", "runtime_status": "STATUS$READY"},
    ])

    async def get_status(cid):
        return next(statuses)

    async def start_ok(cid):
        calls.append("start")
        return {"status": "ok"}

    s.openhands.send_message = send
    s.openhands.get_status = get_status
    s.openhands.start_conversation = start_ok
    s.conversation_ready_timeout = 5  # разрешаем цикл ожидания
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert calls == ["send", "start", "send"]
    assert s.state.get_setting("pr_review_last_id:tk9") == "10"


def test_review_message_contains_author_path_and_instruction():
    feedback = [
        {"id": 10, "author": "alice", "kind": "review:changes_requested",
         "body": "Не хватает тестов."},
        {"id": 11, "author": "bob", "kind": "inline", "path": "a/b.dart",
         "body": "Переименуй переменную."},
    ]
    s = make_scheduler(feedback)
    task = make_done_task()

    async def capture(cid, message):
        s.captured = message
        return {"success": True}

    s.openhands.send_message = capture
    asyncio.run(s._handle_pr_feedback(task, feedback, 11))
    msg = s.captured
    assert "review:changes_requested от @alice" in msg
    assert "inline от @bob (a/b.dart)" in msg
    assert "ПУШИТЬ НЕ НУЖНО" in msg
    assert "Не хватает тестов." in msg


def test_tasks_without_pr_skipped():
    s = make_scheduler([])
    t = make_done_task()
    t.pr_url = "(dry-run) ветка x не отправлена"
    s.state.save(t)
    s._poll_pr_review_feedback()
    assert s.github.feedback_calls == []


# ------------------------------------------------------------------ #
# Адаптер: _gh_api не должен отбрасывать JSON-массивы.
#
# Регрессия: `return data if isinstance(data, dict) else {}` молча
# превращал списковые ответы GitHub (/pulls/{n}/comments и т.п.) в
# пустой dict — fetch_pr_feedback всегда был пуст, хвост ревью не
# работал (PR #67: 2 инлайн-комментария не доставлялись агенту).
# ------------------------------------------------------------------ #

class FakeRequester:
    """Отдаёт данные по префиксу пути, как разные эндпоинты GitHub."""

    def __init__(self, by_prefix: dict[str, object]):
        self._by_prefix = by_prefix

    def requestJsonAndCheck(self, method, url, **kw):
        for prefix, data in self._by_prefix.items():
            if prefix in url:
                return ({}, data)
        return ({}, [])


class FakeRepo:
    url = "https://api.github.com/repos/PrigodaOleg/social_mind"


def make_raw_gh(by_prefix: dict[str, object]) -> GitHubAdapter:
    gh = object.__new__(GitHubAdapter)
    gh._gh = object()
    gh._repo = FakeRepo()
    gh._repo.requester = FakeRequester(by_prefix)
    return gh


def test_gh_api_returns_list_endpoints():
    gh = make_raw_gh({"/pulls/67/comments": [
        {"id": 1, "body": "инлайн"}, {"id": 2, "body": "второй"},
    ]})
    data = gh._gh_api("GET", "/pulls/67/comments")
    assert isinstance(data, list) and len(data) == 2


def test_fetch_pr_feedback_sees_inline_comments():
    gh = make_raw_gh({
        "/issues/67/comments": [],
        "/pulls/67/reviews": [],
        "/pulls/67/comments": [
            {"id": 4454673885, "user": {"login": "PrigodaOleg", "type": "User"},
             "body": "не нужно лить блоб в репозиторий",
             "path": "domains/client/web/isar.wasm"},
        ],
    })
    fb = gh.fetch_pr_feedback(
        "https://github.com/PrigodaOleg/social_mind/pull/67", after_comment_id=0
    )
    assert len(fb) == 1
    assert fb[0]["kind"] == "inline"
    assert fb[0]["path"] == "domains/client/web/isar.wasm"
    assert "блоб" in fb[0]["body"]


# ------------------------------------------------------------------ #
# ci_pending: пока хвост ревью активен (агент правит замечания), карточка
# задачи не должна висеть «✅ завершена» (PR #36).
# ------------------------------------------------------------------ #

def test_review_tail_sets_ci_pending_flag():
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def ok(cid, message):
        return {"success": True}

    async def pushed(*a, **kw):
        return {"id": 123}

    s.openhands.send_message = ok
    s._sync_and_push_ci_fix = pushed
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    # Правки запушены, вотчер заспавнен — флаг остаётся до конца хвоста
    # (снимет _watch_gha_artifacts в finally).
    assert task.ci_pending is True


def test_review_tail_clears_flag_when_no_fixes():
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)

    async def ok(cid, message):
        return {"success": True}

    # _sync_and_push_ci_fix из make_scheduler возвращает None (правок нет).
    s.openhands.send_message = ok
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert task.ci_pending is False


# ------------------------------------------------------------------ #
# Стоимость задачи должна учитываться с работой агента в хвостах
# (автофикс CI, правки по ревью) — токены копятся в разговоре.
# ------------------------------------------------------------------ #

def test_replace_cost_line_updates_existing():
    s = make_scheduler([])
    task = make_done_task()
    task.tokens_prompt, task.tokens_completion, task.cost_rub = 100, 50, 12.5
    body = (
        "✅ Задача выполнена! PR создан: https://github.com/x/y/pull/1\n"
        "Стоимость: 1.00 ₽ (вход 10 / выход 5 ток.)\n"
        "<!-- artifacts -->"
    )
    out = s._replace_cost_line(body, task)
    assert "Стоимость: 12.50 ₽ (вход 100 / выход 50 ток.)" in out
    assert out.count("Стоимость:") == 1
    assert "<!-- artifacts -->" in out


def test_replace_cost_line_without_line_noop():
    s = make_scheduler([])
    task = make_done_task()
    task.cost_rub = 3.0
    body = "Просто текст без стоимости"
    assert s._replace_cost_line(body, task) == body


def test_no_fixes_branch_recomputes_cost():
    """Агент отвечал пояснением (без правок) — стоимость пересчитывается."""
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback)
    task = make_done_task()
    s.state.save(task)
    cost_calls: list[str] = []

    async def ok(cid, message):
        return {"success": True}

    async def record_cost(t):
        cost_calls.append(t.id)

    s.openhands.send_message = ok
    s._finalize_cost = record_cost
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert cost_calls == [task.id]
    assert task.ci_pending is False


def test_no_fixes_but_ci_running_resumes_tail():
    """Пуш вне цикла (руками) с уже идущим CI — хвост продолжается вотчером.

    Регрессия: после ручного пуша с запуском CI хендлер попал в ветку
    «правок нет» и бросил хвост без присмотра (⏳ снят, прогон бесхозный).
    """
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    running = [{"id": 555, "html_url": "http://run/555",
                "status": "in_progress", "conclusion": None}]
    s = make_scheduler(feedback, runs=running)
    task = make_done_task()
    s.state.save(task)
    spawned: list = []
    s._spawn = spawned.append

    async def ok(cid, message):
        return {"success": True}

    async def no_push(*a, **kw):
        return None  # правок нет: коммит-эквивалент уже в ветке

    s.openhands.send_message = ok
    s._sync_and_push_ci_fix = no_push
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert len(spawned) == 1, "вотчер должен быть заспавнен"
    assert task.ci_pending is True


def test_no_fixes_no_ci_ends_tail():
    """Правок нет и CI нет — хвост честно завершён, вотчера нет."""
    feedback = [{"id": 10, "author": "alice", "kind": "issue_comment",
                 "body": "fix"}]
    s = make_scheduler(feedback, runs=[])
    task = make_done_task()
    s.state.save(task)
    spawned: list = []
    s._spawn = spawned.append

    async def ok(cid, message):
        return {"success": True}

    async def no_push(*a, **kw):
        return None

    s.openhands.send_message = ok
    s._sync_and_push_ci_fix = no_push
    asyncio.run(s._handle_pr_feedback(task, feedback, 10))
    assert spawned == []
    assert task.ci_pending is False
