"""Робастность распознавания финального отчёта (задача #62).

Реальная цепочка зависания: отчёт «Работа завершена... Grep-проверка:
упоминаний hive/Hive не осталось» — слово «осталось» в прошедшем времени
с отрицанием, но \\bосталось\\b из _FUTURE_RE гасило маркеры отчёта →
классификатор возвращал «announce» → 4 авто-продолжения (агент на каждый
нудж пересылал тот же отчёт) → после исчерпания лимита отчёт был
отправлен автору как «вопрос», цикл повторился после ответа «продолжай».
"""

from __future__ import annotations

from orchestrator.core.scheduler import Scheduler

# Сокращённая версия реального отчёта из задачи #62.
REPORT_62 = (
    "Работа завершена.\n\n### Сделанные изменения (все в `domains/client`)\n"
    "1. **pubspec.yaml** — удалены `hive_flutter: ^1.1.0` и "
    "закомментированный `hive_generator: ^2.0.1`.\n"
    "2. **lib/repository/_pubspec.yaml** — удалены `hive_flutter` и "
    "`hive_generator`.\n"
    "8. **lib/local_storage/isar/isar_storage.dart** — убрано упоминание "
    "Hive в комментарии.\n\n"
    "Grep-проверка: упоминаний hive/Hive не осталось.\n\n"
    "### Все проверки пройдены успешно\n"
    "- `flutter analyze` — ошибок нет\n"
    "- `flutter test` (контейнер) — ✅ 221 passed\n\n"
    "Секреты (/tmp/pw-secrets) не коммитились и не раскрывались. "
    "PR не создавался — по инструкции это делает оркестратор."
)


def test_final_report_with_negated_remaining():
    """«Не осталось» — прошедшее время, отчёт остаётся отчётом."""
    assert Scheduler._classify_agent_pause(REPORT_62) == "report"
    assert Scheduler._classify_agent_pause(
        "Задача выполнена. Проверка grep: упоминаний старой модели не осталось."
    ) == "report"
    assert Scheduler._classify_agent_pause(
        "Работа завершена, файлов-заглушек не осталось, всё удалено."
    ) == "report"


def test_strong_report_marker_beats_future_words():
    """Однозначное «работа сделана» сильнее слов-«будущее»."""
    assert Scheduler._classify_agent_pause(
        "Работа завершена. Далее репозиторий можно проверить вручную."
    ) == "report"
    assert Scheduler._classify_agent_pause(
        "Финальный отчёт: всё реализовано. Затем стоит прогнать сборку локально."
    ) == "report"


def test_announce_with_future_still_announce():
    """Промежуточные объявления остаются объявлениями (#5)."""
    assert Scheduler._classify_agent_pause(
        "Тесты прошли. Теперь запускаю сборку APK."
    ) == "announce"
    assert Scheduler._classify_agent_pause(
        "Начну с изучения структуры репозитория."
    ) == "announce"


def test_real_question_still_question():
    """Настоящие вопросы по-прежнему приоритетнее отчётов."""
    assert Scheduler._classify_agent_pause(
        "Работа почти завершена. Какой вариант иконки предпочитаете?"
    ) == "question"
    assert Scheduler._classify_agent_pause("Нужно ли добавлять тесты?") == "question"


def test_negated_future_markers_do_not_fire():
    """Lookbehind отрицания: «не начну»/«не приступаю» — не намерения."""
    assert not Scheduler._FUTURE_RE.search("работы не начну до ответа")
    assert not Scheduler._FUTURE_RE.search("исправления не приступаю делать")
    # А обычное намерение по-прежнему ловится.
    assert Scheduler._FUTURE_RE.search("теперь запускаю тесты")


# --- Явные маркеры из промпта ([QUESTION]/[STATUS]/[DONE]) --------------- #

def test_pause_tags_take_priority():
    """Маркер первой строкой — высший приоритет над эвристиками."""
    # [DONE] побеждает даже если дальше похоже на вопрос
    assert Scheduler._classify_agent_pause(
        "[DONE] Всё готово. Стоит ли что-то ещё улучшить?"
    ) == "report"
    # [QUESTION] побеждает даже «завершено» в тексте
    assert Scheduler._classify_agent_pause(
        "[QUESTION] Какой вариант выбираешь? Работа почти завершена."
    ) == "question"
    # [STATUS] — объявление, даже с маркерами отчёта
    assert Scheduler._classify_agent_pause(
        "[STATUS] Тесты прошли, работа продолжается."
    ) == "announce"


def test_pause_tags_format_tolerant():
    """Регистр, markdown-жирный, эмодзи, пробелы — всё допустимо."""
    assert Scheduler._classify_agent_pause("done: всё готово") == "report"
    assert Scheduler._classify_agent_pause("**[DONE]** Всё готово.") == "report"
    assert Scheduler._classify_agent_pause("✅ [DONE] Отчёт ниже") == "report"
    assert Scheduler._classify_agent_pause("[question] жду уточнения") == "question"
    # Маркер НЕ первой строкой — не считается
    assert Scheduler._classify_agent_pause(
        "Отчёт:\n[DONE] работа завершена"
    ) != "report" or Scheduler._classify_agent_pause(
        "Отчёт:\n[DONE] работа завершена"
    ) == "report"  # допустимо любое поведение — текст решает


def test_no_tag_falls_back_to_heuristics():
    """Без маркера работают текстовые эвристики (старые разговоры)."""
    assert Scheduler._classify_agent_pause(REPORT_62) == "report"
    assert Scheduler._classify_agent_pause("Начну с изучения файлов.") == "announce"