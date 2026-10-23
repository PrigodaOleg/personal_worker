#!/usr/bin/env python3
"""Дождаться, пока воркеру будет безопасно останавливаться (вызывается деплоем).

Два источника «занятости»:

1. Активные задачи в БД (state/orchestrator.sqlite): статусы
   new / ready / clarifying / running. Задачи выполняются по одной,
   так что ждём, пока очередь и текущая задача закончатся.
2. «Хвосты» — фоновые вотчеры GHA-сборок (_watch_gha_artifacts): они живут
   ПОСЛЕ завершения задачи (статус уже done) и правят финальный комментарий
   в issue. Планировщик кладёт маркер state/gha_watchers/<run_id>.watch на
   время вотчера. Маркеры старше таймаута слежения считаются протухшими
   (вотчер сам выходит по deadline и удаляет маркер; протухший маркер
   остаётся только после сбоя/перезапуска сервиса).

Если сервис не запущен — ждать нечего, выходим сразу.
По истечении --max-wait-seconds падаем с ошибкой: деплой не должен молча
убивать работающую задачу. Решение подождать дольше — за человеком.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import time
from pathlib import Path

# Статусы задач, при которых воркер занят (TaskStatus).
ACTIVE_STATUSES = ("new", "ready", "clarifying", "running")
# GHA_WATCH_TIMEOUT_SECONDS по умолчанию + запас: маркер старше этого
# возраста оставлен мёртвым вотчером (сбой сервиса) и не считается занятостью.
STALE_WATCHER_SECONDS = 5400 + 600


def service_active(name: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True  # не смогли проверить — пусть ждёт по общим правилам


def busy_tasks(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return [{"id": "?", "status": "db-locked"}]
    try:
        rows = conn.execute(
            f"SELECT id, status FROM tasks WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "status": r[1]} for r in rows]


def busy_watchers(watchers_dir: Path) -> list[dict]:
    if not watchers_dir.is_dir():
        return []
    now = time.time()
    out = []
    for marker in watchers_dir.glob("*.watch"):
        age = now - marker.stat().st_mtime
        if age > STALE_WATCHER_SECONDS:
            continue  # протухший маркер: вотчер мёртв
        out.append({"id": marker.stem, "age_min": round(age / 60)})
    return out


def snapshot(db_path: Path, watchers_dir: Path) -> tuple[list[dict], list[dict]]:
    return busy_tasks(db_path), busy_watchers(watchers_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--max-wait-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--service", default="personal_worker")
    args = parser.parse_args()

    db_path = args.project / "state" / "orchestrator.sqlite"
    watchers_dir = args.project / "state" / "gha_watchers"

    if not service_active(args.service):
        print(f"сервис {args.service} не активен — ждать нечего")
        return 0

    deadline = time.monotonic() + args.max_wait_seconds
    while True:
        tasks, watchers = snapshot(db_path, watchers_dir)
        if not tasks and not watchers:
            print("✅ воркер свободен: активных задач и GHA-хвостов нет")
            return 0
        left = int(deadline - time.monotonic())
        if left <= 0:
            print(
                f"❌ воркер занят дольше {args.max_wait_seconds}s: "
                f"задач={len(tasks)} ({[t['id'] for t in tasks]}), "
                f"gha-хвостов={len(watchers)} ({[w['id'] for w in watchers]}). "
                "Деплой прерван, сервис не тронут. Подождите завершения "
                "работы и запустите деплой ещё раз."
            )
            return 1
        watcher_info = [(w["id"], f"{w['age_min']}m") for w in watchers]
        print(
            f"… жду: задач={len(tasks)} {[(t['id'], t['status']) for t in tasks]} "
            f"gha-хвостов={len(watchers)} {watcher_info} "
            f"(осталось ждать {left}s)",
            flush=True,
        )
        time.sleep(min(args.poll_seconds, max(left, 1)))


if __name__ == "__main__":
    raise SystemExit(main())
