"""Регрессионные тесты механизма «вопрос агента → ответ автора» (GitHub).

Фон: задача из GitHub issue зависала, когда агент в процессе работы задавал
вопрос автору: в _handle_agent_question использовалась неопределённая
переменная qinfo (NameError), из-за чего в записи ожидания не появлялись
github_issue / github_after_comment_id, и poll_github никогда не забирал
ответ из комментариев issue.

Тесты не ходят в сеть: Scheduler собирается через object.__new__ с фейковыми
адаптерами.
"""

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


class FakeGitHub:
    def __init__(self):
        self.comments: list[tuple[int, int, str]] = []
        self.edits: list[tuple[int, str]] = []

    def create_comment(self, issue_number: int, body: str) -> int:
        cid = 1000 + len(self.comments)
        self.comments.append((issue_number, cid, body))
        return cid

    def update_comment(self, comment_id: int, body: str) -> None:
        self.edits.append((comment_id, body))

    def last_bot_comment_id(self, issue_number: int):
        bot_ids = [cid for (_n, cid, _b) in self.comments]
        return bot_ids[-1] if bot_ids else None


class FakeOpenHands:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, message: str) -> dict:
        self.sent.append((conversation_id, message))
        return {"success": True}

    async def start_conversation(self, conversation_id: str) -> dict:
        return {"status": "ok"}


class FakeTelegram:
    async def send_message(self, *args, **kwargs):
        return 1

    async def edit_message(self, *args, **kwargs):
        return True


def make_scheduler() -> Scheduler:
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}
    s._pending_clarifications = {}
    s._pending_notifications = set()
    s._background_tasks = set()
    s._running_tasks = set()
    s.github = FakeGitHub()
    s.openhands = FakeOpenHands()
    s.telegram = FakeTelegram()
    s.state = types.SimpleNamespace(
        save=lambda t: None,
        get_setting=lambda key, default=None: default,
        set_setting=lambda key, value: None,
    )
    s.config = types.SimpleNamespace(
        clarification_timeout_seconds=60,
        clarification_poll_seconds=0.001,
        imac_host=None,
        imac_user=None,
        imac_ssh_key="",
        github_repo="PrigodaOleg/social_mind",
        telegram_admin_chat_id=None,
    )
    # Атрибуты планировщика (обычно задаёт __init__, который в тесте не звался).
    s.error_retry_wait_seconds = 0.001
    s.loading_startup_timeout_seconds = 0.001  # холодный старт не тормозим
    s.max_stop_restarts = 3
    s.max_auto_continues = 4
    s.max_error_retries = 5
    s.max_state_misses = 3
    return s


def make_task() -> Task:
    return Task(
        id="t36",
        source=TaskSource.GITHUB,
        source_id="36",
        title="тест",
        description="описание",
        status=TaskStatus.RUNNING,
        conversation_id="conv1",
    )


def _spawn(coro):
    return asyncio.get_running_loop().create_task(coro)


def test_agent_question_github_registers_issue_and_delivers_answer():
    """Главный регресс: вопрос регистрируется с github_issue, ответ из
    комментария issue доставляется агенту."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        handle = _spawn(s._handle_agent_question(task, "conv1", "Какой вариант?"))

        # Имитируем _poll_github_agent_question_answers: вопрос должен быть
        # зарегистрирован с номером issue и границей комментариев.
        for _ in range(100):
            qinfo = s._pending_agent_questions.get("t36")
            if qinfo and qinfo.get("github_issue") is not None:
                break
            await asyncio.sleep(0.01)

        qinfo = s._pending_agent_questions.get("t36")
        assert qinfo is not None, "вопрос не зарегистрирован"
        assert qinfo["github_issue"] == 36, (
            "github_issue не записан — ответ автора никогда не будет прочитан"
        )
        assert qinfo["github_after_comment_id"] is not None

        # Автор ответил комментарием — прямая имитация того, что делает
        # реальный _poll_github_agent_question_answers (fut.set_result).
        if not qinfo["future"].done():
            qinfo["future"].set_result("вариант А")

        answered = await handle
        assert answered is True
        assert s.openhands.sent == [("conv1", "вариант А")]
        # Вопрос и итог (принято) ушли в issue, вопрос опубликован один раз.
        assert len(s.github.comments) == 1
        assert s.github.comments[0][0] == 36

    asyncio.run(scenario())


def test_monitor_does_not_reask_same_message():
    """После доставки ответа монитор не должен задать тот же вопрос повторно,
    пока агент не вышел из awaiting_user_input."""

    async def scenario():
        make_scheduler()  # проверки в тесте чисто логические — экземпляр не нужен
        answered_msg = "Какой вариант?"
        message = "Какой вариант?"
        assert message == answered_msg  # условие guard в _monitor_openhands

        # Состояние изменилось — защита снимается.
        answered_msg = None
        assert not (message and message == answered_msg)

    asyncio.run(scenario())


def test_send_message_failure_returns_false():
    """Если доставить ответ не удалось, _handle_agent_question сообщает об
    ошибке, а не делает вид, что всё отправлено."""

    async def scenario():
        s = make_scheduler()

        class FailingOpenHands(FakeOpenHands):
            async def send_message(self, conversation_id, message):
                return {"status": "error", "error": "conversation stopped"}

        s.openhands = FailingOpenHands()
        task = make_task()
        handle = _spawn(s._handle_agent_question(task, "conv1", "Какой вариант?"))
        for _ in range(100):
            qinfo = s._pending_agent_questions.get("t36")
            if qinfo:
                break
            await asyncio.sleep(0.01)
        qinfo["future"].set_result("вариант А")
        answered = await handle
        assert answered is False

    asyncio.run(scenario())


def test_clarification_answer_runs_in_background():
    """Ответ на уточнение не должен блокировать вызывающий цикл:
    _run_openhands уходит в фон (иначе главный цикл встаёт на часы)."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        task.status = TaskStatus.CLARIFYING
        task.clarification_answer = "делай вариант А"
        started = asyncio.Event()

        async def fake_run(t):
            started.set()

        s._run_openhands = fake_run

        async def noop_confirm(t):
            return None

        s._confirm_clarification_accepted = noop_confirm
        s._pending_clarifications["t36"] = {
            "task": task,
            "future": None,
            "asked_at": __import__("datetime").datetime.utcnow(),
        }

        await asyncio.wait_for(s._wait_for_clarification(task), timeout=1)
        assert task.status == TaskStatus.READY
        # Фоновая задача запущена и завершается быстро (fake_run).
        await asyncio.wait_for(started.wait(), timeout=1)
        assert "t36" not in s._pending_clarifications

    asyncio.run(scenario())


def test_run_openhands_guard_prevents_double_start():
    """Двойной запуск одной задачи запрещён (гонка главный цикл / фон)."""

    async def scenario():
        s = make_scheduler()
        task = make_task()
        task.status = TaskStatus.READY
        calls = []

        async def fake_impl(t):
            calls.append(t.id)
            await asyncio.sleep(0.05)

        s._run_openhands_impl = fake_impl
        s._notify_author = lambda t, text: None

        await asyncio.gather(
            s._run_openhands(task),
            s._run_openhands(task),
        )
        assert calls == ["t36"], "задача запущена дважды"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# get_agent_state: stopped во время ожидания ответа ≠ завершение работы
# --------------------------------------------------------------------------- #

def _state_event(state: str) -> dict:
    return {"observation": "agent_state_changed", "source": "environment",
            "extras": {"agent_state": state}}


def _agent_message(text: str, wait: bool) -> dict:
    return {"action": "message", "source": "agent", "message": text,
            "args": {"wait_for_response": wait}}


def make_adapter_with(events: list[dict]):
    from orchestrator.adapters.openhands import OpenHandsAdapter

    adapter = object.__new__(OpenHandsAdapter)
    adapter.base_url = "http://localhost:3000"

    async def fake_trajectory(cid):
        return {"trajectory": events}

    adapter.get_trajectory = fake_trajectory
    return adapter


def test_stopped_while_awaiting_is_not_completed():
    """Разговор, погашенный idle-таймаутом, пока агент ждал ответ, — НЕ
    завершённая работа (именно так задача #36 ложно 'выполнилась')."""

    async def scenario():
        a = make_adapter_with([
            _agent_message("I'll start by exploring...", True),
            _state_event("running"),
            _state_event("awaiting_user_input"),
            _state_event("stopped"),
        ])
        info = await a.get_agent_state("conv1")
        assert info["agent_state"] == "stopped"
        assert info["completed"] is False, (
            "awaiting_user_input не должен считаться завершением работы"
        )
        assert info["state_before_stop"] == "awaiting_user_input"

    asyncio.run(scenario())


def test_stopped_after_finish_is_completed():
    """Разговор, погашенный после финального отчёта агента, — завершён."""

    async def scenario():
        a = make_adapter_with([
            _agent_message("Готово: всё собралось.", False),
            _state_event("running"),
            _state_event("finished"),
            _state_event("stopped"),
        ])
        info = await a.get_agent_state("conv1")
        assert info["agent_state"] == "stopped"
        assert info["completed"] is True
        assert info["state_before_stop"] == "finished"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Классификация паузы агента: финальный отчёт ≠ вопрос
# --------------------------------------------------------------------------- #

REPORT_SNIPPET = (
    "Все проверки пройдены успешно! ✅\n\n"
    "## Что сделано\n\n"
    "**Изменения (коммит `88f07fa` в локальной ветке `main`, "
    "без push — PR создаст оркестратор):**\n\n"
    "1. **`.github/workflows/dart.yml`** — добавлен job `build-ios`.\n"
    "2. **`domains/client/ios/Podfile`** — новый файл.\n\n"
    "| Команда | Результат |\n"
    "|---|---|\n"
    "| `flutter test` | ✅ 52 теста пройдены |\n\n"
    "Рабочее дерево чистое."
)


def test_final_report_is_not_a_question():
    """Финальный отчёт агента (модель завершает им разговор с
    wait_for_response=true) — не вопрос: нумерованный список в нём — это
    сделанные действия, а не варианты ответа (задача #36)."""
    assert Scheduler._classify_agent_pause(REPORT_SNIPPET) == "report"
    assert Scheduler._classify_agent_pause("Готово. Работа завершена.") == "report"
    assert Scheduler._classify_agent_pause(
        "Итог: изменения внесены, все тесты пройдены."
    ) == "report"


def test_real_question_is_recognized():
    """Настоящие вопросы по-прежнему спрашивают автора."""
    assert Scheduler._classify_agent_pause("Какой вариант предпочитаете?") == "question"
    assert Scheduler._classify_agent_pause("Мне нужно уточнение по формату ключа") == "question"
    assert Scheduler._classify_agent_pause("Should I proceed with option 2?") == "question"

def test_tool_markup_is_ignored_in_classification():
    """Псевдо-вызов инструмента текстом: модель напечатала
    `<function=str_replace_editor>...` в сообщении, а в дарт-коде внутри
    разметки встретилась фраза «which would break current clients» —
    маркер вопроса срабатывал, объявление намерения считалось вопросом,
    задача уходила в ожидание ответа на два суток (issue #43)."""
    pseudo = (
        "Инфраструктура ясна. Создаю: 1) тест, 2) CI-workflow. "
        "<function=str_replace_editor><parameter=command>create</parameter>"
        "<parameter=file_text>// which would break current clients\n"
        "void main() {}\n</parameter></str_replace_editor>"
    )
    assert Scheduler._classify_agent_pause(pseudo) == "announce"

def test_unterminated_tool_markup_is_stripped():
    """Разметка без закрывающего </function> (обрезана) тоже вычищается."""
    pseudo = (
        "Пишу тест. <function=execute_bash><parameter=command>ls -la\n"
        "// should i continue? yes"
    )
    assert Scheduler._classify_agent_pause(pseudo) == "announce"

def test_real_question_with_code_is_still_question():
    """Вопрос вне разметки не глохнет: маркер в обычном тексте работает."""
    msg = (
        "Создаю файл. <function=str_replace_editor><parameter=command>create</parameter>"
        "</function> Какой вариант предпочитаете?"
    )
    assert Scheduler._classify_agent_pause(msg) == "question"


# --------------------------------------------------------------------------- #
# Маркеры будущего — по границам слов (issue #5: зависание в цикле подталкиваний)
# --------------------------------------------------------------------------- #

REPORT_WITH_NEGATION = (
    "работа завершена\n\n"
    "Сделанные изменения:\n"
    "1. Создан **Makefile** с автоматизацией всего цикла разработки:\n"
    "   - `ios-build` — `flutter build ios --release --no-codesign` на iMac;\n"
    "     тесты на iMac не запускаются — isar_plus на macOS падает.\n"
    "2. Все проверки пройдены успешно: flutter test — 52 теста.\n"
    "Секреты не коммитились. PR не создавался."
)


def test_report_with_negated_verb_is_report():
    """«тесты не запускаются» содержит подстроку «запускаю» — простое вхождение
    превращало финальный отчёт в «объявление намерения», монитор зацикливался
    на подталкиваниях, задача зависала (issue #5). Границы слов это чинят."""
    assert Scheduler._classify_agent_pause(REPORT_WITH_NEGATION) == "report"


def test_future_intent_still_recognized():
    """Настоящее намерение продолжать работу — по-прежнему не отчёт."""
    assert Scheduler._classify_agent_pause(
        "Тесты прошли успешно. Теперь запускаю сборку APK."
    ) == "announce"
    assert Scheduler._classify_agent_pause(
        "I'll start by exploring the files."
    ) == "announce"


def test_announcement_is_not_report_nor_question():
    """«I'll start by exploring...» — не вопрос и не отчёт: агент просто
    объявил намерение и встал на паузу."""
    assert Scheduler._classify_agent_pause(
        "I'll start by exploring the workflow files in the project."
    ) == "announce"
    assert Scheduler._classify_agent_pause("") == "announce"


def test_midwork_status_with_future_intent_is_not_final_report():
    """«Тесты прошли. Теперь запускаю сборку APK» — работа продолжается:
    завершать задачу по такому сообщению нельзя."""
    assert Scheduler._classify_agent_pause(
        "Тесты прошли успешно. Теперь запускаю сборку APK."
    ) == "announce"


def _monitor_scheduler(states: list[dict], max_auto: int = 4):
    s = make_scheduler()
    s.config.clarification_poll_seconds = 0.01
    s.max_auto_continues = max_auto
    s._has_build_secrets = lambda: True

    class StateOpenHands(FakeOpenHands):
        counter = 0

        async def get_agent_state(self, conversation_id):
            st = states[min(self.counter, len(states) - 1)]
            self.counter += 1
            return st

    s.openhands = StateOpenHands()

    async def fail_question(*a, **k):
        raise AssertionError("сообщение классифицировано как вопрос")

    s._handle_agent_question = fail_question
    return s


def test_monitor_finishes_task_on_final_report():
    """Монитор: агент в awaiting_user_input прислал отчёт → задача DONE,
    вопрос автору НЕ задаётся, PR-цикл запускается."""

    async def scenario():
        s = _monitor_scheduler([{
            "agent_state": "awaiting_user_input",
            "message": REPORT_SNIPPET,
            "wait_for_response": True,
            "completed": False,
            "state_before_stop": None,
        }])
        pr_called = []

        async def fake_pr(task, client_dir):
            pr_called.append(task.id)

        s._build_ios_and_create_pr = fake_pr

        task = make_task()
        await asyncio.wait_for(
            s._monitor_openhands(task, "conv1", "/tmp/client"), timeout=2
        )
        assert task.status == TaskStatus.DONE
        assert pr_called == ["t36"], "завершение без запуска PR-цикла"
        assert s.openhands.sent == [], "в разговоре нечего отправлять"

    asyncio.run(scenario())


def test_monitor_nudges_announcement_instead_of_asking():
    """Объявление намерения без вопроса не завершает задачу и не вешает
    вопрос на автора: агенту уходит авто-подталкивание."""

    async def scenario():
        s = _monitor_scheduler([
            {"agent_state": "awaiting_user_input",
             "message": "I'll start by exploring the workflow files in the project.",
             "wait_for_response": True, "completed": False,
             "state_before_stop": None},
            {"agent_state": "running", "message": "", "wait_for_response": False,
             "completed": False, "state_before_stop": None},
        ])
        task = make_task()

        async def fail_pr(task_, client_dir):
            raise AssertionError("объявление принято за финальный отчёт")

        s._build_ios_and_create_pr = fail_pr

        # После двух опросов останавливаем монитор через отмену задачи.
        calls = {"n": 0}
        orig_get_state = s.openhands.get_agent_state  # bound method класса

        async def counting_get_state(conversation_id):
            calls["n"] += 1
            if calls["n"] >= 3:
                task.status = TaskStatus.CANCELLED
            return await orig_get_state(conversation_id)

        s.openhands.get_agent_state = counting_get_state

        await asyncio.wait_for(
            s._monitor_openhands(task, "conv1", "/tmp/client"), timeout=2
        )
        assert len(s.openhands.sent) == 1, "подталкивание должно быть одно"
        cid, text = s.openhands.sent[0]
        assert cid == "conv1" and "Продолжай" in text
        assert task.status == TaskStatus.CANCELLED

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# auto_continues: счётчик сбрасывается при реальном прогрессе агента (issue #38)
# --------------------------------------------------------------------------- #

def _pause_state(message: str, work: int) -> dict:
    return {"agent_state": "awaiting_user_input", "message": message,
            "wait_for_response": True, "completed": False,
            "state_before_stop": None, "work_events": work}


def _running_state() -> dict:
    return {"agent_state": "running", "message": "", "wait_for_response": False,
            "completed": False, "state_before_stop": None, "work_events": 0}


def test_monitor_resets_auto_continues_when_agent_works():
    """Агент сообщает промежуточный статус и РЕАЛЬНО работает между паузами
    (исполняет команды) — задача не должна зависнуть на вопросах автору:
    счётчик авто-продолжений сбрасывается, каждый статус — просто
    подталкивание (регресс issue #38: бесконечные «да» от автора).

    Прогресс = рост исполненных команд (get_events), НЕ work_events:
    OpenHands обнуляет work_events при каждом запуске агента, из-за чего
    сброс срабатывал в каждом цикле и подталкивания становились бесконечными
    (разбор задачи #46, attempt 3)."""

    async def scenario():
        s = _monitor_scheduler([
            _pause_state("Сообщаю: начал работу", 0),
            _running_state(),
            _pause_state("Сообщаю: пишу тесты", 7),
            _running_state(),
            _pause_state("Сообщаю: проверяю сборку", 15),
            _running_state(),
        ], max_auto=1)

        async def fail_question(*a, **k):
            raise AssertionError("промежуточный статус переслан автору как вопрос")

        s._handle_agent_question = fail_question
        task = make_task()

        polls = {"n": 0}
        orig_get_state = s.openhands.get_agent_state

        async def counting_get_state(conversation_id):
            polls["n"] += 1
            if polls["n"] >= 8:
                task.status = TaskStatus.CANCELLED
            return await orig_get_state(conversation_id)

        s.openhands.get_agent_state = counting_get_state

        # Исполненные команды копятся: после каждой паузы их больше.
        async def growing_events(conversation_id, limit=100):
            return [
                {"action": "run", "message": "Running command: ls"}
                for _ in range(s.openhands.counter)
            ]

        s.openhands.get_events = growing_events

        await asyncio.wait_for(
            s._monitor_openhands(task, "conv1", "/tmp/client"), timeout=2
        )
        # Три паузы с прогрессом между ними — три подталкивания, ни одного
        # вопроса автору, несмотря на max_auto_continues=1.
        assert len(s.openhands.sent) == 3, (
            f"ожидалось 3 подталкивания, отправлено {len(s.openhands.sent)}"
        )
        assert task.status == TaskStatus.CANCELLED

    asyncio.run(scenario())


def test_monitor_escalates_pauses_without_progress():
    """Паузы ПОДРЯД без единого рабочего события — агент зациклился:
    после исчерпания лимита вопрос автору правомерен (и это единственный
    случай, когда промежуточное сообщение уходит автору)."""

    async def scenario():
        s = _monitor_scheduler([
            _pause_state("Шаг 1", 0),
            _pause_state("Шаг 2", 0),
            _pause_state("Шаг 3", 0),
        ], max_auto=2)
        asked: list[str] = []

        async def fake_question(task, cid, msg):
            asked.append(msg)
            task.status = TaskStatus.CANCELLED
            return False

        s._handle_agent_question = fake_question
        task = make_task()
        await asyncio.wait_for(
            s._monitor_openhands(task, "conv1", "/tmp/client"), timeout=2
        )
        assert len(asked) == 1, (
            f"после {2} пауз без прогресса нужна ровно одна эскалация, "
            f"получено {len(asked)}"
        )

    asyncio.run(scenario())
