"""Тесты инъекции SSH-ключа iMac в runtime-контейнер.

Агент сам проверяет iOS-сборку на iMac по SSH из контейнера; ключ кладётся
только в /tmp/pw-imac-key (в workspace нельзя — уехал бы в git и в PR).
"""

import asyncio
import types

from orchestrator.core.scheduler import Scheduler


def make_scheduler() -> Scheduler:
    s = object.__new__(Scheduler)
    s._pending_agent_questions = {}
    s._pending_clarifications = {}
    s._pending_notifications = set()
    s._background_tasks = set()
    s._running_tasks = set()
    s._task_queue = asyncio.Queue()
    s._queue_worker_started = False
    return s


def make_config(tmp_path, key_name: str = "imac_key"):
    key = tmp_path / key_name
    key.write_text("SSHKEY")
    return types.SimpleNamespace(
        imac_host="192.168.88.20",
        imac_user="worker",
        imac_ssh_key=str(key),
    )


def test_imac_key_injected_into_runtime(tmp_path):
    """Ключ копируется в /tmp/pw-imac-key и получает права 600."""

    async def scenario():
        s = make_scheduler()
        s.config = make_config(tmp_path)
        calls = []

        async def fake_docker(*args, timeout=60):
            calls.append(args)
            if args[0] == "inspect":
                return 0, "true\n", ""
            return 0, "", ""

        s._docker = fake_docker

        ok = await asyncio.wait_for(
            s._ensure_imac_ssh_in_runtime("conv123"), timeout=2
        )
        assert ok is True
        container = "openhands-runtime-conv123"
        assert ("cp", s.config.imac_ssh_key, f"{container}:/tmp/pw-imac-key") in calls
        assert ("exec", container, "chmod", "600", "/tmp/pw-imac-key") in calls

    asyncio.run(scenario())


def test_imac_key_skipped_when_not_configured(tmp_path):
    """iMac не настроен — ничего не делаем, успех (шаг необязательный)."""

    async def scenario():
        s = make_scheduler()
        s.config = types.SimpleNamespace(
            imac_host=None,
            imac_user=None,
            imac_ssh_key=str(tmp_path / "missing"),
        )
        calls = []

        async def fake_docker(*args, timeout=60):
            calls.append(args)
            return 0, "", ""

        s._docker = fake_docker
        ok = await asyncio.wait_for(
            s._ensure_imac_ssh_in_runtime("conv"), timeout=2
        )
        assert ok is True
        assert calls == [], "docker не должен вызываться без настройки iMac"

    asyncio.run(scenario())


def test_imac_key_false_when_container_not_running(tmp_path):
    """Контейнер не запущен — False (повторить позже из мониторинга)."""

    async def scenario():
        s = make_scheduler()
        s.config = make_config(tmp_path)

        async def fake_docker(*args, timeout=60):
            if args[0] == "inspect":
                return 0, "false\n", ""
            return 0, "", ""

        s._docker = fake_docker
        ok = await asyncio.wait_for(
            s._ensure_imac_ssh_in_runtime("conv"), timeout=5
        )
        assert ok is False

    asyncio.run(scenario())
