"""Реплики агента в Telegram-логе задачи (блок «Что делает агент»).

_collect_task_logs раньше показывал только команды ($ cmd → exit N).
Теперь между ними видны и текстовые реплики агента (💬) — понятно, ЗАЧЕМ
команды выполняются. Служебный шум и псевдо-вызовы инструментов
выфильтровываются.
"""

from __future__ import annotations

import asyncio

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource


class FakeOpenHands:
    def __init__(self, events):
        self._events = events

    async def get_events(self, conversation_id, limit=100):
        return self._events


def make_scheduler(events) -> Scheduler:
    s = object.__new__(Scheduler)
    s.openhands = FakeOpenHands(events)
    return s


def make_task() -> Task:
    return Task(
        id="t1",
        source=TaskSource.TELEGRAM,
        source_id="1",
        title="тест",
        description="",
        conversation_id="conv1",
    )


def run(s: Scheduler, task: Task) -> str:
    return asyncio.run(s._collect_task_logs(task, lines=10))


def test_remarks_shown_between_commands():
    events = [
        {"source": "agent", "message": "Проверю workflows на уровне репозитория."},
        {"action": "run", "args": {"command": "ls .github/workflows"}},
        {"observation": "run", "extras": {
            "command": "ls .github/workflows",
            "metadata": {"exit_code": 0},
        }},
        {"source": "agent", "message": "Workflows нашёл, теперь изучу тесты правил."},
    ]
    out = run(make_scheduler(events), make_task())
    assert "💬 Проверю workflows на уровне репозитория." in out
    assert "$ ls .github/workflows" in out
    assert "→ exit 0" in out
    assert "💬 Workflows нашёл, теперь изучу тесты правил." in out
    # порядок хронологический
    assert out.index("💬 Проверю") < out.index("$ ls") < out.index("💬 Workflows")


def test_noise_and_user_messages_not_shown_as_remarks():
    events = [
        {"source": "agent", "message": "Running command: ls -la"},
        {"source": "agent", "message": "Command `ls -la` executed with exit code 0."},
        {"source": "agent", "message": "I am thinking...: текущее состояние — правила лежат в lib."},
        {"source": "agent", "message": "Your thought has been logged."},
        {"source": "agent", "message": "Code executed in IPython cell."},
        {"source": "environment", "message": "No observation"},
        {"source": "user", "message": "Продолжай выполнение задачи по исходной инструкции."},
        {"source": "agent", "message": "Настоящая реплика агента."},
    ]
    out = run(make_scheduler(events), make_task())
    assert out == "💬 Настоящая реплика агента."


def test_tool_markup_stripped_from_remark():
    events = [
        {
            "source": "agent",
            "message": (
                "Инфраструктура ясна. Создаю: 1) тест, 2) CI-workflow. "
                "<function=str_replace_editor><parameter=command>create</parameter>"
                "<parameter=file_text>// which would break clients\n"
                "void main() {}\n</parameter></str_replace_editor>"
            ),
        },
    ]
    out = run(make_scheduler(events), make_task())
    assert "💬 Инфраструктура ясна. Создаю: 1) тест, 2) CI-workflow." in out
    assert "<function=" not in out
    assert "which would" not in out


def test_markup_only_message_skipped():
    events = [
        {
            "source": "agent",
            "message": "<function=execute_bash><parameter=command>ls</parameter></function>",
        },
    ]
    out = run(make_scheduler(events), make_task())
    assert out == "— агент ещё не выполнял команд и не писал реплик"


def test_long_remark_truncated():
    events = [
        {"source": "agent", "message": "x" * 500},
    ]
    out = run(make_scheduler(events), make_task())
    assert out.startswith("💬 ")
    assert "…" in out
    assert len(out) < 200


def test_running_command_still_marked():
    events = [
        {"source": "agent", "message": "Запускаю сборку APK."},
        {"action": "run", "args": {"command": "flutter build apk"}},
    ]
    out = run(make_scheduler(events), make_task())
    assert "💬 Запускаю сборку APK." in out
    assert "🔄 flutter build apk — выполняется…" in out


def test_no_conversation_placeholder():
    s = make_scheduler([])
    out = asyncio.run(s._collect_task_logs(None))
    assert "не запускалась" in out
