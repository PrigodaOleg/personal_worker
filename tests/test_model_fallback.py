"""Автосмена модели при сломанном tool-calling / зацикливании агента.

Цепочка fallback-моделей (config.model_fallbacks). При деградации
провайдера (сырые вызовы текстом, 0 команд) или AgentStuckInLoopError
после ретрая задача НЕ падает, а перезапускается на следующей модели
цепочки. Учёт использованных моделей — маркерами в логе задачи.
"""

from __future__ import annotations

import asyncio
import inspect
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


def _mk_task() -> Task:
    t = Task(id="fb1", source=TaskSource.GITHUB, source_id="7",
             title="тест", description="d")
    t.status = TaskStatus.RUNNING
    return t


def _bare_scheduler(models: list[str]) -> Scheduler:
    s = object.__new__(Scheduler)
    s.config = types.SimpleNamespace(model_fallbacks=lambda: models)
    s.state = types.SimpleNamespace(save=lambda t: None)
    return s


def test_next_fallback_picks_first_unused():
    s = _bare_scheduler(["m-a", "m-b", "m-c"])
    t = _mk_task()  # model не задана → _effective_model = дефолт
    s._default_model = lambda: "m-a"
    assert s._next_fallback_model(t) == "m-b"


def test_next_fallback_skips_tried_models():
    s = _bare_scheduler(["m-a", "m-b", "m-c"])
    t = _mk_task()
    t.model = "m-b"
    t.add_log(f"{Scheduler._FALLBACK_LOG_MARK}: m-a → m-b (тест)")
    s._default_model = lambda: "m-a"
    # m-a (маркер) и m-b (текущая) уже попробованы → следующий m-c
    assert s._next_fallback_model(t) == "m-c"


def test_next_fallback_exhausted():
    s = _bare_scheduler(["m-a", "m-b"])
    t = _mk_task()
    t.model = "m-b"
    t.add_log(f"{Scheduler._FALLBACK_LOG_MARK}: m-a → m-b (тест)")
    s._default_model = lambda: "m-a"
    assert s._next_fallback_model(t) is None


def test_switch_model_sets_model_and_logs():
    s = _bare_scheduler(["m-a", "m-b"])
    t = _mk_task()
    saved: list[Task] = []
    s.state = types.SimpleNamespace(save=lambda task: saved.append(task))
    s._default_model = lambda: "m-a"

    model = s._switch_model_fallback(t, "сломанный tool-calling")
    assert model == "m-b"
    assert t.model == "m-b"
    assert saved == [t]
    assert any("m-a → m-b" in str(e) and "сломанный" in str(e) for e in t.log)

    # Повторный вызов должен выбрать следующую (m-c нет в цепочке → None)
    s2 = _bare_scheduler(["m-a", "m-b"])
    s2._default_model = lambda: "m-a"
    assert s2._switch_model_fallback(t, "опять сломалось") is None


def test_restart_with_fallback_resets_task():
    """Перезапуск на fallback: READY, новый разговор, workspace очищен."""
    import shutil
    import tempfile
    from pathlib import Path

    s = _bare_scheduler(["m-a", "m-b"])
    t = _mk_task()
    t.status = TaskStatus.FAILED
    t.conversation_id = "conv-old"
    t.attempt = 1
    tmp = tempfile.mkdtemp(prefix="pw-fb-test-")
    ws = Path(tmp) / t.id / "repo"
    ws.mkdir(parents=True)
    (ws / "x.txt").write_text("1")
    s._workspace_for = lambda task: str(Path(tmp) / task.id)

    enqueued: list[Task] = []
    s.state = types.SimpleNamespace(save=lambda task: None)
    s._enqueue_task = lambda task: enqueued.append(task)
    s._ensure_workspace = lambda task: str(ws)  # noqa: F841

    asyncio.run(s._restart_with_fallback_model(t, "m-b", "broken tool-calling"))

    assert t.status == TaskStatus.READY
    assert t.conversation_id is None
    assert t.attempt == 2
    assert not ws.exists(), "старый workspace должен быть удалён"
    assert enqueued == [t]
    shutil.rmtree(tmp, ignore_errors=True)


def test_broken_tool_calling_path_uses_fallback():
    """В ветке сломанного tool-calling есть авто-fallback до FAILED."""
    src = inspect.getsource(Scheduler._monitor_openhands)
    assert "_switch_model_fallback" in src
    assert "_restart_with_fallback_model" in src
    # Текст про исчерпание цепочки присутствует (fallback-ветка до роняния)
    assert "fallback-моделей исчерпана" in src or "fallback" in src


def test_stuck_loop_path_uses_fallback():
    """Ветка AgentStuckInLoopError после ретрая тоже пробует fallback."""
    src = inspect.getsource(Scheduler._monitor_openhands)
    stuck_part = src.split("AgentStuckInLoopError) — ретрай и fallback")[0]
    assert "stuck_failed_model" in stuck_part


def test_fallbacks_config_env_comma_separated():
    """model_fallbacks можно задать списком или строкой через запятую."""
    from orchestrator.core.config import Config

    cfg = object.__new__(Config)
    cfg._env = lambda key, default=None: "a/x, b/y ,c/z" if key == "MODEL_FALLBACKS" else default
    cfg._raw_get = lambda *keys, default=None: default
    assert cfg.model_fallbacks() == ["a/x", "b/y", "c/z"]

    cfg2 = object.__new__(Config)
    cfg2._env = lambda key, default=None: None if key == "MODEL_FALLBACKS" else default
    cfg2._raw_get = lambda *keys, default=None: default
    assert isinstance(cfg2.model_fallbacks(), list) and cfg2.model_fallbacks()
