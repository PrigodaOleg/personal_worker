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
    "// AI-TASK-E2E-RUN и больше ничего в коде не меняй."
)


async def main() -> int:
    config = Config(env_path=".env", yaml_path="orchestrator/config.yaml")
    logger.info("PR_DRY_RUN = %s", config.pr_dry_run)

    scheduler = Scheduler(config)
    # Поднимаем бота на отправку, чтобы уведомление о результате реально ушло.
    await scheduler.telegram.connect()

    task = Task(
        id="e2e-dryrun",
        source=TaskSource.TELEGRAM,
        source_id="91741245",
        title="Сквозной прогон (dry-run)",
        description=DESCRIPTION,
        status=TaskStatus.NEW,
    )
    scheduler.state.save(task)

    logger.info("Запускаю задачу %s", task.id)
    await scheduler._run_openhands(task)
    await scheduler.flush_notifications()

    logger.info("Итоговый статус: %s", task.status)
    for line in task.log:
        logger.info("  %s", line)

    await scheduler.telegram.close()
    return 0 if task.status == TaskStatus.DONE else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
