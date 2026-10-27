"""CI-хвост задачи: ⏳ пока GHA-вотчер жив, ✅/❌ только после него.

Пользовательская проблема (PR #53/#54): задача становилась ✅ «выполнена»
сразу после создания PR, хотя CI на PR красный и вотчер ещё крутил цикл
автоисправления — «зелёный» статус сбивал с толку.

Плюс: устаревшие кнопки меню («📊 Статус», «📤 Подтолкнуть») из
неперезапущенной клавиатуры клиента создавали задачи-мусор.
"""

from __future__ import annotations

import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


def make_task(status=TaskStatus.DONE) -> Task:
    return Task(
        id="tk-tail",
        source=TaskSource.GITHUB,
        source_id="54",
        title="e2e на ios",
        description="",
        status=status,
        pr_url="https://github.com/o/r/pull/54",
    )


def test_done_with_ci_tail_shows_pending():
    """done + живой GHA-вотчер → ⏳ «CI-проверка PR», а не ✅."""
    s = object.__new__(Scheduler)
    t = make_task()
    t.ci_pending = True
    emoji, label = s._display_status(t)
    assert (emoji, label) == ("⏳", "CI-проверка PR")


def test_done_without_tail_is_green():
    """Хвост закончен → обычное ✅ «выполнена»."""
    s = object.__new__(Scheduler)
    t = make_task()
    t.ci_pending = False
    emoji, label = s._display_status(t)
    assert (emoji, label) == ("✅", "выполнена")


def test_failed_task_not_affected_by_ci_tail():
    """FAILED не перекрашивается хвостом: ❌ остаётся ❌."""
    s = object.__new__(Scheduler)
    t = make_task(TaskStatus.FAILED)
    t.ci_pending = True
    emoji, label = s._display_status(t)
    assert (emoji, label) == ("❌", "ошибка")


def test_task_card_shows_ci_pending():
    """В карточке задачи виден хвост: «Статус: CI-проверка PR»."""
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}

    saved: dict = {}

    class FakeState:
        def save(self, task):
            saved[task.id] = task

        def get_setting(self, key, default=None):
            return default

    s.state = FakeState()
    s._models_cache = []
    s.config = types.SimpleNamespace(openhands_llm_model="test-model")
    t = make_task()
    t.ci_pending = True
    card = s._task_card(t)
    assert "CI-проверка PR" in card
    assert "✅" not in card.split("\n")[0]


def test_retired_menu_buttons_do_not_create_tasks():
    """Тексты старых кнопок меню не доходят до создания задачи."""
    retired = Scheduler._RETIRED_MENU_BUTTONS
    assert "📊 Статус" in retired
    assert "📤 Подтолкнуть" in retired
    # Пункты актуального меню НЕ должны числиться устаревшими.
    from orchestrator.adapters.telegram import MENU_COMMANDS

    assert not (set(MENU_COMMANDS) & set(retired)), (
        "кнопка не может быть одновременно в меню и устаревшей"
    )
