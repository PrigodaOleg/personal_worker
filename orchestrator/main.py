"""Точка входа оркестратора personal_worker.

Запуск:
    python -m orchestrator.main
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from orchestrator.core.config import Config
from orchestrator.core.scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("personal_worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="personal_worker — orchestrator for Flutter tasks"
    )
    parser.add_argument(
        "--config",
        default="orchestrator/config.yaml",
        help="Путь к config.yaml",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Путь к .env",
    )
    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = Config(env_path=args.env, yaml_path=args.config)
    scheduler = Scheduler(config)

    await scheduler.run()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
