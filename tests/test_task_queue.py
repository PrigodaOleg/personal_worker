"""Тесты глобальной очереди задач: строго одна за раз.

Слабое железо: локальные docker-сборки и один iMac не тянут параллельную
работу агентов. Все источники (Telegram, GitHub, восстановление после
сбоя) встают в одну очередь; воркер дожидается фактического освобождения
слота — запуск агента после уточнения уходит в фон и держит task.id в
_running_tasks до завершения.
"""

import asyncio
import types

from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus


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


def make_task(tid: str) -> Task:
    return Task(
        id=tid,
        source=TaskSource.TELEGRAM,
        source_id="1",
        title=tid,
        description="описание задачи",
        status=TaskStatus.NEW,
    )


def test_queue_runs_one_at_a_time():
    """Две задачи в очереди — конкурентность не превышает 1."""

    async def scenario():
        s = make_scheduler()
        state = {"cur": 0, "max": 0}

        async def fake_process(task):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.05)
            state["cur"] -= 1

        s._process_task = fake_process

        s._enqueue_task(make_task("t1"))
        s._enqueue_task(make_task("t2"))
        await asyncio.wait_for(s._task_queue.join(), timeout=2)
        assert state["max"] == 1, f"задачи выполнялись параллельно (max={state['max']})"

    asyncio.run(scenario())


def test_queue_waits_for_background_slot():
    """_process_task вернулся, а работа ушла в фон (_running_tasks): воркер
    берёт следующую задачу только после освобождения слота."""

    async def scenario():
        s = make_scheduler()
        events = []

        async def fake_process(task):
            events.append(f"start:{task.id}")
            if task.id == "t1":
                # Имитация _run_openhands: работа ушла в фон и держит слот.
                s._running_tasks.add(task.id)

                async def finish():
                    await asyncio.sleep(0.1)
                    s._running_tasks.discard(task.id)
                    events.append(f"slot-free:{task.id}")

                s._spawn(finish())
            # Возврат сразу — как после подтверждения уточнения.

        s._process_task = fake_process

        s._enqueue_task(make_task("t1"))
        s._enqueue_task(make_task("t2"))
        await asyncio.wait_for(s._task_queue.join(), timeout=3)
        assert events.index("slot-free:t1") < events.index("start:t2"), (
            f"вторая задача началась до освобождения слота: {events}"
        )

    asyncio.run(scenario())


def test_process_task_restarts_dead_conversation():
    """RUNNING без живого разговора: попытка+1, READY, перезапуск через очередь."""

    async def scenario():
        s = make_scheduler()
        task = make_task("t1")
        task.status = TaskStatus.RUNNING
        task.conversation_id = "conv"
        task.attempt = 0
        s.state = types.SimpleNamespace(get=lambda tid: task, save=lambda t: None)

        class OH:
            async def get_agent_state(self, cid):
                return {"error": "not found"}

        s.openhands = OH()
        s.max_resume_attempts = 3
        s._notify_author = lambda t, text: None
        s._notify_admin = lambda text: None

        async def noop_cost(t):
            return None

        s._finalize_cost = noop_cost
        runs = []

        async def fake_run(t):
            runs.append(t.id)

        s._run_openhands = fake_run

        await asyncio.wait_for(s._process_task(task), timeout=2)
        assert task.attempt == 1, "попытка не увеличена"
        assert task.status == TaskStatus.READY
        assert runs == ["t1"], "перезапуск не выполнен"

    asyncio.run(scenario())


def test_process_task_reconnects_live_conversation():
    """RUNNING с живым разговором: монитор переподключается, без перезапуска."""

    async def scenario():
        s = make_scheduler()
        task = make_task("t1")
        task.status = TaskStatus.RUNNING
        task.conversation_id = "conv"
        task.attempt = 0
        s.state = types.SimpleNamespace(get=lambda tid: task, save=lambda t: None)

        class OH:
            async def get_agent_state(self, cid):
                return {"agent_state": "awaiting_user_input"}

        s.openhands = OH()
        s._ensure_workspace = lambda t: "/tmp/ws"
        monitored = []

        async def fake_monitor(t, cid, cdir):
            monitored.append(cid)

        s._monitor_openhands = fake_monitor

        await asyncio.wait_for(s._process_task(task), timeout=2)
        assert monitored == ["conv"], "монитор не переподключился"
        assert task.attempt == 0, "попытку зря увеличили"

    asyncio.run(scenario())


def test_process_task_attempt_limit_fails():
    """Исчерпаны попытки перезапуска — FAILED, запуск не выполняется."""

    async def scenario():
        s = make_scheduler()
        task = make_task("t1")
        task.status = TaskStatus.RUNNING
        task.attempt = 3
        s.state = types.SimpleNamespace(get=lambda tid: task, save=lambda t: None)
        s.max_resume_attempts = 3
        s._notify_author = lambda t, text: None
        s._notify_admin = lambda text: None

        async def noop_cost(t):
            return None

        s._finalize_cost = noop_cost
        runs = []

        async def fake_run(t):
            runs.append(t.id)

        s._run_openhands = fake_run
        await asyncio.wait_for(s._process_task(task), timeout=2)
        assert task.status == TaskStatus.FAILED
        assert runs == []

    asyncio.run(scenario())
