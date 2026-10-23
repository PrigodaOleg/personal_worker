"""Уведомление админа в Telegram о новом issue с меткой задачи.

poll_github забирает issue с меткой aitask и ставит задачу в очередь.
Админ должен получить в Telegram уведомление о каждом НОВОМ issue —
причём ровно один раз: при следующих поллах задача уже есть в state,
и дублей быть не должно.
"""

import asyncio
import types

from orchestrator.core.scheduler import Scheduler


class FakeGitHub:
    def __init__(self, issues):
        self._issues = issues

    def fetch_open_issues(self):
        return self._issues


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 1


def make_scheduler(issues) -> tuple[Scheduler, list, FakeTelegram]:
    s = object.__new__(Scheduler)
    s._pending_notifications = set()
    s._background_tasks = set()
    enqueued = []
    s.github = FakeGitHub(issues)
    s.telegram = FakeTelegram()
    s.state = types.SimpleNamespace(get=lambda _id: None, save=lambda t: None)
    s.config = types.SimpleNamespace(
        github_label="aitask",
        telegram_admin_chat_id="111",
    )
    # Тяжёлые части опроса не тестируем здесь — глушим.
    s._cancel_tasks_for_closed_issues = lambda: None
    s._poll_github_clarification_answers = lambda: None
    s._poll_github_agent_question_answers = lambda: None
    s._enqueue_task = enqueued.append
    return s, enqueued, s.telegram


def _drain(s: Scheduler):
    """Дождаться фоновых отправок: _notify_admin уходит через ensure_future."""
    return asyncio.gather(*list(s._pending_notifications), return_exceptions=True)


def test_new_issue_notifies_admin_once():
    async def scenario():
        issues = [
            {
                "number": 41,
                "title": "Сломался экран на iOS",
                "body": "описание",
                "url": "https://github.com/PrigodaOleg/social_mind/issues/41",
                "author": "prigodaoleg",
            }
        ]
        s, enqueued, tg = make_scheduler(issues)

        await s.poll_github()
        await _drain(s)

        assert len(enqueued) == 1  # задача встала в очередь
        assert len(tg.sent) == 1  # одно уведомление админу
        chat_id, text = tg.sent[0]
        assert chat_id == "111"
        assert "Сломался экран на iOS" in text
        assert "prigodaoleg" in text
        assert "issues/41" in text  # ссылка на issue
        assert "aitask" in text

        # Второй полл: задача уже в state → без новых уведомлений и очереди.
        s.state.get = lambda _id: object()  # задача найдена
        await s.poll_github()
        assert len(enqueued) == 1
        assert len(tg.sent) == 1

    asyncio.run(scenario())


def test_two_issues_two_notifications():
    async def scenario():
        issues = [
            {"number": 1, "title": "a", "body": "", "url": "u1", "author": "x"},
            {"number": 2, "title": "b", "body": "", "url": "u2", "author": None},
        ]
        s, enqueued, tg = make_scheduler(issues)

        await s.poll_github()
        await _drain(s)

        assert len(enqueued) == 2
        assert len(tg.sent) == 2
        # Автор неизвестен — вместо login подставляется unknown.
        assert "(автор: unknown)" in tg.sent[1][1]

    asyncio.run(scenario())
