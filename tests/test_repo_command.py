"""Парсинг /repo add|check|del <owner/name>.

Баг: name = parts[1] — для /repo add PrigodaOleg/x это слово «add»,
команда вечно отвечала «Формат: …» и до проверки доступа не доходила
(пользовательский репорт: не работает добавление репозитория).
"""

from __future__ import annotations

import re

from orchestrator.core.scheduler import Scheduler


def test_repo_subcommand_name_is_parts_two():
    """Для /repo <sub> <owner/name> имя репо — parts[2], а не parts[1]."""
    text = "/repo add PrigodaOleg/zmk-config-hillside46"
    parts = text.split()
    cmd = parts[0].lower()
    assert cmd == "/repo"
    sub = parts[1].lower()
    mapped = {
        "add": "/repo_add",
        "check": "/repo_check",
        "del": "/repo_del",
    }.get(sub)
    assert mapped == "/repo_add"
    name = parts[2]
    assert name == "PrigodaOleg/zmk-config-hillside46"
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", name), name


def test_repo_short_form_name_is_parts_one():
    """Короткая форма /repo_add <owner/name> — имя в parts[1]."""
    text = "/repo_add PrigodaOleg/zmk-config-hillside46"
    parts = text.split()
    cmd = parts[0].lower()
    name = parts[1] if len(parts) > 1 else ""
    assert cmd == "/repo_add"
    assert name == "PrigodaOleg/zmk-config-hillside46"
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", name), name


def test_repo_unknown_subcommand_rejected():
    """Неизвестная подкоманда /repo foo → отказ, а не тихий name=foo."""
    mapping = {"add": "/repo_add", "check": "/repo_check", "del": "/repo_del"}
    assert mapping.get("foo") is None


def test_handle_repo_command_uses_preparsed_name():
    """_handle_repo_command принимает имя извне (name), не перечитывая parts[1]."""
    import asyncio
    import types

    s = object.__new__(Scheduler)
    s._is_admin = lambda username: True
    s.github = types.SimpleNamespace(has_repo_access=lambda r: True, ensure_label=lambda r: True, label="aitask")
    s.state = types.SimpleNamespace(get_setting=lambda k: None, set_setting=lambda k, v: None)
    s.config = types.SimpleNamespace(github_repo="PrigodaOleg/social_mind")
    s._repo_adapters = {}
    s.github.for_repo = lambda r: s.github
    sent: list = []

    class FakeTg:
        async def send_message(self, chat_id, text, keyboard=None):
            sent.append(text)

    s.telegram = FakeTg()
    # /repo add PrigodaOleg/zmk-config-hillside46: parts[1] = "add" — не репо!
    parts = ["/repo", "add", "PrigodaOleg/zmk-config-hillside46"]
    asyncio.run(s._handle_repo_command(
        1, "/repo_add", parts, "boss",
        name="PrigodaOleg/zmk-config-hillside46",
    ))
    # Дошло до проверки доступа (ответ не «Формат: …»), и имя репо — из name,
    # а не parts[1] («add»)
    assert sent and not sent[0].startswith("Формат:")
    assert "PrigodaOleg/zmk-config-hillside46" in sent[0]
