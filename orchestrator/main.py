"""Точка входа оркестратора personal_worker.

Запуск:
    python -m orchestrator.main
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from orchestrator.core.scheduler import Scheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="personal_worker — orchestrator for Flutter tasks"
    )
    parser.add_argument(
        "--source",
        choices=["telegram", "github", "all"],
        default="all",
        help="Источник задач (по умолчанию все)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Интервал опроса в секундах (по умолчанию 30)",
    )
    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    scheduler = Scheduler(
        sources=args.source.split(","),
        poll_interval=args.poll_interval,
    )
    await scheduler.run()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
