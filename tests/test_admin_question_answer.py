"""Вопросы агента в Telegram у админа: уведомление и ответ из чата.

Для задач из issue вопрос уходит автору в issue, а админу — в Telegram:
уведомление с кнопками вариантов + «Свой ответ» (qask). Ответить можно
и из сводки статуса (кнопка «✍️ Ответить на вопрос»), и из списка задач
(метка ❓).
"""

from __future__ import annotations

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []
        self.callback_answers: list[str] = []

    async def send_message(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))
        return 1

    async def edit_message(self, *a, **kw):
        return True

    async def answer_callback_query(self, callback_query_id, text=""):
        self.callback_answers.append(text)

    @staticmethod
    def build_keyboard(rows):
        from orchestrator.adapters.telegram import TelegramAdapter
        return TelegramAdapter.build_keyboard(rows)


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[int, str]] = []
        self.edits: list[tuple[int, str]] = []

    def create_comment(self, issue_number, body):
        self.comments.append((issue_number, body))
        return 7000 + len(self.comments)

    def update_comment(self, comment_id, body):
        self.edits.append((comment_id, body))


class FakeOpenHands:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, conversation_id, message):
        self.sent.append((conversation_id, message))
        return {"success": True}

    async def get_events(self, conversation_id, limit=100):
        return []


def make_scheduler(github_repo="PrigodaOleg/social_mind") -> tuple:
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}
    s._pending_clarifications = {}
    s._pending_notifications = set()
    s._background_tasks = set()
    s._admin_answer_chat = {}
    s.github = FakeGitHub()
    s.openhands = FakeOpenHands()
    s.telegram = FakeTelegram()
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        get=lambda _id: None,
        get_setting=lambda key, default=None: default,
        set_setting=lambda key, value: None,
    )
    s.config = types.SimpleNamespace(
        clarification_timeout_seconds=5,
        clarification_poll_seconds=0.01,
        telegram_admin_chat_id="999",
        telegram_admin_username="boss",
        github_repo=github_repo,
        openhands_llm_model="glm-5.3-flash",
    )
    s._repo_adapters = {}
    return s


def make_github_task() -> Task:
    return Task(
        id="tq1",
        source=TaskSource.GITHUB,
        source_id="43",
        title="firebase rules",
        description="",
        conversation_id="conv1",
    )


def kb_data(keyboard) -> list[str]:
    """callback_data всех кнопок клавиатуры."""
    if keyboard is None:
        return []
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


def kb_texts(keyboard) -> list[str]:
    if keyboard is None:
        return []
    return [b.text for row in keyboard.inline_keyboard for b in row]


# --------------------------------------------------------------------------- #
# Уведомление админа о вопросе
# --------------------------------------------------------------------------- #
def test_github_question_notifies_admin_with_buttons():
    async def scenario():
        s = make_scheduler()
        task = make_github_task()

        # Вопрос с вариантами — фоновая отправка админу; даём handle
        # зарегистрировать вопрос и отправить уведомление.
        handle = asyncio.ensure_future(
            s._handle_agent_question(
                task, "conv1", "Какой вариант окружения?\n1. staging\n2. prod"
            )
        )
        await asyncio.sleep(0.05)
        # Отвечаем через «нажатие» кнопки варианта, как это сделал бы админ.
        query = {"data": "ans:tq1:0", "callback_query_id": "cq1",
                 "chat_id": 999, "message_id": 1, "username": "boss"}
        await s._on_telegram_callback(query)
        assert await handle is True

        # Админское уведомление ушло: текст вопроса + кнопки.
        admin_msgs = [m for m in s.telegram.sent if m[0] == 999]
        assert admin_msgs, "админу не отправлено уведомление"
        _, text, kb = admin_msgs[0]
        assert "firebase rules" in text and "Какой вариант" in text
        assert "ans:tq1:0" in kb_data(kb)
        assert "qask:tq1" in kb_data(kb)

    asyncio.run(scenario())


def test_no_admin_notification_without_admin_chat():
    async def scenario():
        s = make_scheduler()
        s.config.telegram_admin_chat_id = None
        task = make_github_task()

        handle = asyncio.ensure_future(
            s._handle_agent_question(task, "conv1", "Уточни формат ключа?")
        )
        await asyncio.sleep(0.05)
        assert not [m for m in s.telegram.sent if m[0] == 999]
        # Вопрос при этом висит; закрываем через отмену задачи.
        task.status = TaskSource.TELEGRAM and task.status
        s._pending_agent_questions["tq1"]["future"].cancel()
        with TypeError_expected():
            pass
        try:
            await handle
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


class TypeError_expected:
    def __enter__(self):
        pass

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------- #
# Ответ админа из Telegram (qask → следующее сообщение)
# --------------------------------------------------------------------------- #
def test_admin_answer_via_qask_mode():
    async def scenario():
        s = make_scheduler()
        task = make_github_task()
        fut = asyncio.get_running_loop().create_future()
        s._pending_agent_questions["tq1"] = {
            "task": task, "question": "Какой вариант?", "options": [],
            "future": fut, "asked_at": __import__("datetime").datetime.utcnow(),
        }

        # 1. Нажатие «✍️ Свой ответ» админом — режим включается.
        await s._on_telegram_callback({
            "data": "qask:tq1", "callback_query_id": "cq2",
            "chat_id": 999, "message_id": 2, "username": "boss",
        })
        assert s._admin_answer_chat.get("999") == "tq1"

        # 2. Следующее сообщение админа — ответ резолвится в future.
        # (Доставку агенту делает await _handle_agent_question; в тесте
        # обработчика нет — проверяем, что future получил текст.)
        await s._on_telegram_message({
            "chat_id": "999", "username": "boss",
            "text": "используй staging и не трогай prod",
        })
        assert fut.done() and fut.result() == "используй staging и не трогай prod"
        assert "tq1" not in s._admin_answer_chat  # режим одноразовый

    asyncio.run(scenario())


def test_qask_rejected_for_non_admin():
    async def scenario():
        s = make_scheduler()
        s._pending_agent_questions["tq1"] = {
            "task": make_github_task(), "question": "q?", "options": [],
            "future": asyncio.get_running_loop().create_future(),
            "asked_at": __import__("datetime").datetime.utcnow(),
        }
        await s._on_telegram_callback({
            "data": "qask:tq1", "callback_query_id": "cq3",
            "chat_id": 555, "message_id": 3, "username": "hacker",
        })
        assert "555" not in s._admin_answer_chat
        assert "🔒" in s.telegram.callback_answers[-1]

    asyncio.run(scenario())


def test_admin_can_cancel_answer_mode():
    async def scenario():
        s = make_scheduler()
        fut = asyncio.get_running_loop().create_future()
        s._pending_agent_questions["tq1"] = {
            "task": make_github_task(), "question": "q?", "options": [],
            "future": fut, "asked_at": __import__("datetime").datetime.utcnow(),
        }
        s._admin_answer_chat["999"] = "tq1"
        await s._on_telegram_message({
            "chat_id": "999", "username": "boss", "text": "×",
        })
        assert not fut.done()  # ответ не отправлен
        assert "999" not in s._admin_answer_chat

    asyncio.run(scenario())


def test_answer_mode_consumes_once_when_question_closed():
    async def scenario():
        s = make_scheduler()
        s._pending_agent_questions["tq1"] = {
            "task": make_github_task(), "question": "q?", "options": [],
            "future": None, "asked_at": __import__("datetime").datetime.utcnow(),
        }
        s._admin_answer_chat["999"] = "tq1"
        await s._on_telegram_message({
            "chat_id": "999", "username": "boss", "text": "поздний ответ",
        })
        assert "999" not in s._admin_answer_chat
        assert any("уже закрыт" in t for _, t, _k in s.telegram.sent)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Висящие вопросы в сводке статуса и списке
# --------------------------------------------------------------------------- #
def test_status_view_shows_pending_question_and_button():
    async def scenario():
        s = make_scheduler()
        fut = asyncio.get_running_loop().create_future()
        s._pending_agent_questions["tq1"] = {
            "task": make_github_task(),
            "question": "Какой вариант: <function=x>which would</function> staging?",
            "options": [], "future": fut,
            "asked_at": __import__("datetime").datetime.utcnow(),
        }
        task = make_github_task()
        task.status = __import__("orchestrator.core.task", fromlist=["TaskStatus"]).TaskStatus.RUNNING

        async def fake_layers(t):
            return "—"

        s._layer_status_block = fake_layers
        text, kb = await s._render_status_view(task)
        assert "❓ Ждёт ответа" in text
        assert "Какой вариант" in text
        assert "<function=" not in text  # разметка вычищена
        assert "qask:tq1" in kb_data(kb)

    asyncio.run(scenario())


def test_status_view_clean_when_no_question():
    async def scenario():
        s = make_scheduler()
        task = make_github_task()
        task.status = __import__("orchestrator.core.task", fromlist=["TaskStatus"]).TaskStatus.RUNNING

        async def fake_layers(t):
            return "—"

        s._layer_status_block = fake_layers
        text, kb = await s._render_status_view(task)
        assert "Ждёт ответа" not in text
        assert "qask:tq1" not in kb_data(kb)

    asyncio.run(scenario())


def test_task_list_marks_pending_question():
    async def scenario():
        s = make_scheduler()
        fut = asyncio.get_running_loop().create_future()
        s._pending_agent_questions["tq1"] = {
            "task": make_github_task(), "question": "q?", "options": [],
            "future": fut, "asked_at": __import__("datetime").datetime.utcnow(),
        }
        s.state.list_recent = lambda limit=50: [make_github_task()]
        await s._send_task_list(999, "999", 0)
        text = s.telegram.sent[-1][1]
        assert "❓" in text

    asyncio.run(scenario())
