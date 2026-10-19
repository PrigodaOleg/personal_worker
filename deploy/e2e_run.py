"""Одноразовый сквозной прогон пайплайна personal_worker.

Прогоняет задачу целиком: OpenHands -> синхронизация правок -> git ->
git push --dry-run (PR_DRY_RUN=1) без создания ветки и PR на GitHub.

Запуск:
    PR_DRY_RUN=1 .venv/bin/python deploy/e2e_run.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.core.config import Config
from orchestrator.core.scheduler import Scheduler
from orchestrator.core.task import Task, TaskSource, TaskStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e")

DESCRIPTION = (
    "Внеси минимальную правку: добавь в самый конец файла "
    "domains/client/lib/main.dart одну строку-комментарий "
    "// AI-TASK-E2E-RUN и больше ничего в коде не меняй. "
    "Никакие команды сборки/анализа не запускай — только правка файла."
)


async def main() -> int:
    config = Config(env_path=".env", yaml_path="orchestrator/config.yaml")
    logger.info("PR_DRY_RUN = %s", config.pr_dry_run)

    scheduler = Scheduler(config)
    # Только на отправку: автономный прогон без поллинга.
    await scheduler.telegram.connect()

    task = Task(
        id="e2e-clean",
        source=TaskSource.TELEGRAM,
        source_id="91741245",
        title="Сквозной прогон (dry-run, чистый)",
        description=DESCRIPTION,
        status=TaskStatus.NEW,
    )
    scheduler.state.save(task)

    async def auto_answer() -> None:
        """Автоматически отвечать на вопросы агента, чтобы прогон не зависал."""
        while True:
            for _tid, info in list(scheduler._pending_agent_questions.items()):
                fut = info["future"]
                if fut and not fut.done():
                    opts = info.get("options") or []
                    answer = opts[0] if opts else "Продолжай на своё усмотрение."
                    fut.set_result(answer)
                    logger.info("auto-answer → %s", answer[:40])
            await asyncio.sleep(3)

    responder = asyncio.create_task(auto_answer())
    logger.info("Запускаю задачу %s", task.id)
    await scheduler._run_openhands(task)
    responder.cancel()
    await scheduler.flush_notifications()

    logger.info("Итоговый статус: %s | цена: %.2f ₽ | токены %s/%s", task.status,
                task.cost_rub, task.tokens_prompt, task.tokens_completion)
    for line in task.log:
        logger.info("  %s", line)

    await scheduler.telegram.close()
    return 0 if task.status == TaskStatus.DONE else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
