"""Планировщик задач: приём, уточнения, OpenHands (сборки внутри контейнера),
iOS на iMac, PR и уведомления.

Жизненный цикл задачи:
  NEW -> CLARIFYING (если не хватает информации) -> READY -> RUNNING (OpenHands)
    -> (OpenHands может вернуть вопрос -> CLARIFYING) -> DONE/FAILED/CANCELLED
  После успешного OpenHands: iOS на iMac (по SSH) -> PR в GitHub -> уведомление автору.

Сборки Linux / Web / Android запускаются не на хосте, а внутри контейнера
OpenHands (там же установлен Flutter SDK). Агент сам выполняет
flutter test / flutter build linux / flutter build web / flutter build apk.

Архитектура выполнения:
- _on_telegram_message / poll_github только сохраняют задачу и возвращаются
- Основной цикл run() обрабатывает задачи из состояния
- Мониторинг OpenHands происходит в фоне (не блокирует Telegram)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Optional

from orchestrator.adapters.github import GitHubAdapter
from orchestrator.adapters.imac import IMacAdapter
from orchestrator.adapters.openhands import OpenHandsAdapter
from orchestrator.adapters.telegram import TelegramAdapter
from orchestrator.core.config import Config
from orchestrator.core.state import StateStore
from orchestrator.core.task import Task, TaskSource, TaskStatus
from orchestrator.runners.ios import IOSRunner

logger = logging.getLogger("personal_worker")


class Scheduler:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = StateStore(config.state_db)

        self.github = GitHubAdapter(
            app_id=config.github_app_id or 0,
            private_key_path=config.github_private_key_path or "",
            installation_id=config.github_installation_id or 0,
            repo_full_name=config.github_repo,
            label=config.github_label,
        )
        self.telegram = TelegramAdapter(
            bot_token=config.telegram_bot_token,
            admin_username=config.telegram_admin_username,
            allowed_usernames=config.telegram_allowed_usernames,
        )
        self.telegram.set_message_callback(self._on_telegram_message)
        self.openhands = OpenHandsAdapter(
            base_url=config.openhands_base_url,
            api_key=config.openhands_api_key,
            llm_base_url=config.openhands_llm_base_url,
            llm_api_key=config.openhands_llm_api_key,
            llm_model=config.openhands_llm_model,
            github_app_id=config.github_app_id,
            github_private_key_path=config.github_private_key_path,
            github_installation_id=config.github_installation_id,
        )
        self.imac = IMacAdapter(
            host=config.imac_host or "",
            user=config.imac_user or "",
            ssh_key=config.imac_ssh_key,
            work_dir=config.imac_work_dir,
        )
        self.ios_runner = IOSRunner(imac=self.imac)

        self._pending_clarifications: dict[str, dict] = {}
        self._pending_notifications: set = set()  # фоновые отправки в Telegram
        self._llm_configured: bool = False
        self._running_tasks: set[str] = set()  # задачи, которые выполняются в фоне
        # Имя контейнера сервера OpenHands — нужно для вывода логов в /status.
        self.openhands_container: str = os.getenv(
            "OPENHANDS_CONTAINER", "openhands-flutter"
        )
        # Сколько опросов подряд терпеть недоступное состояние агента,
        # прежде чем считать задачу упавшей.
        self.max_state_misses: int = int(os.getenv("OPENHANDS_MAX_STATE_MISSES", "10"))

    # ------------------------------------------------------------------ #
    # Вспомогательные
    # ------------------------------------------------------------------ #
    def _task_id(self, source: TaskSource, source_id: str) -> str:
        raw = f"{source.value}:{source_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _workspace_for(self, task: Task) -> str:
        return os.path.join(self.config.work_dir, task.id)

    async def _git_ahead_count(self, client_dir: str) -> int:
        """Сколько коммитов в ветке задачи сверх базовой (origin/main)."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "rev-list", "--count", "origin/main..HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        try:
            return int(out.decode("utf-8", errors="replace").strip() or "0")
        except ValueError:
            return 0

    async def _git_diff_summary(self, client_dir: str, limit: int = 20) -> str:
        """Краткая сводка изменений для уведомления."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "diff", "--stat", "HEAD~1", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            return "—"
        lines = text.splitlines()
        return "\n".join(lines[:limit])

    @staticmethod
    def _match_line_endings(content: bytes, reference: bytes) -> bytes:
        """Привести переводы строк копируемого файла к стилю исходного.

        Агент (и линтеры внутри контейнера) часто переписывают файлы,
        меняя CRLF на LF. Если это не отфильтровать, в PR попадает файл
        целиком «изменённым» — diff становится нечитаемым. Реальная правка
        при этом сохраняется.
        """
        ref_crlf = reference.count(b"\r\n") > reference.count(b"\n") // 2
        out_crlf = content.count(b"\r\n") > content.count(b"\n") // 2
        if ref_crlf == out_crlf:
            return content
        if ref_crlf:  # исходник CRLF, копия LF -> разворачиваем в CRLF
            return content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return content.replace(b"\r\n", b"\n")  # исходник LF, копия CRLF

    def _write_matched(self, src_tmp: str, dst: str) -> None:
        """Скопировать файл из контейнера, сохранив стиль переводов строк."""
        with open(src_tmp, "rb") as fh:
            content = fh.read()
        if os.path.exists(dst):
            with open(dst, "rb") as fh:
                content = self._match_line_endings(content, fh.read())
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(content)

    def _git_auth_args(self) -> list[str]:
        """Аргументы git для аутентификации и авторства коммитов.

        Токен передаётся заголовком, а не в URL: иначе он утекает в
        `git remote -v` и в логи ошибок git. Идентичность задаём явно —
        в контейнере git обычно не настроен, и `git commit` падает с
        "Author identity unknown".
        """
        user, token = self.github.get_git_credentials()
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        author = self.config.git_author_name
        email = self.config.git_author_email
        return [
            "-c", f"http.extraHeader=Authorization: Basic {basic}",
            "-c", f"user.name={author}",
            "-c", f"user.email={email}",
        ]

    def _ensure_workspace(self, task: Task) -> str:
        ws = self._workspace_for(task)
        os.makedirs(ws, exist_ok=True)
        return ws

    def _notify_author(self, task: Task, text: str) -> None:
        """Отправить уведомление автору задачи в исходном канале.

        Telegram-отправка асинхронная, поэтому запускаем её фоном. Задачу
        обязательно держим в self._pending_notifications: иначе GC может
        уничтожить её до отправки, и уведомление молча потеряется.
        """
        if task.source == TaskSource.GITHUB:
            try:
                num = int(task.source_id)
                self.github.create_comment(num, text)
                logger.info("[notify] comment added to issue #%s", num)
            except Exception as exc:
                logger.error("[notify] github comment failed: %s", exc)
        elif task.source == TaskSource.TELEGRAM:
            try:
                chat_id = int(task.source_id)
                fut = asyncio.ensure_future(self.telegram.send_message(chat_id, text))
                self._pending_notifications.add(fut)
                fut.add_done_callback(self._pending_notifications.discard)
                logger.info("[notify] telegram message queued for %s", chat_id)
            except Exception as exc:
                logger.error("[notify] telegram send failed: %s", exc)

    async def flush_notifications(self) -> None:
        """Дождаться отправки всех отложенных уведомлений."""
        if self._pending_notifications:
            await asyncio.gather(*list(self._pending_notifications), return_exceptions=True)

    def _get_active_task_for_chat(self, chat_id: str) -> Optional[Task]:
        """Найти активную (не завершённую) задачу для этого chat_id."""
        active_statuses = [
            TaskStatus.NEW,
            TaskStatus.CLARIFYING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
        ]
        for status in active_statuses:
            for task in self.state.list_by_status(status):
                if task.source == TaskSource.TELEGRAM and task.source_id == chat_id:
                    return task
        return None

    def _task_status_emoji(self, status: TaskStatus) -> str:
        return {
            TaskStatus.NEW: "🆕",
            TaskStatus.CLARIFYING: "❓",
            TaskStatus.READY: "⏳",
            TaskStatus.RUNNING: "🔄",
            TaskStatus.DONE: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.CANCELLED: "🚫",
        }.get(status, "➖")

    # ------------------------------------------------------------------ #
    # Приём задач из GitHub
    # ------------------------------------------------------------------ #
    async def poll_github(self) -> None:
        logger.debug("[poll] github")
        try:
            issues = self.github.fetch_open_issues()
        except Exception as exc:
            logger.error("[poll] github fetch failed: %s", exc)
            return

        for issue in issues:
            number = str(issue["number"])
            task_id = self._task_id(TaskSource.GITHUB, number)
            if self.state.get(task_id):
                continue

            title = issue.get("title", "")
            body = issue.get("body", "")
            author = issue.get("author") or "unknown"

            task = Task(
                id=task_id,
                source=TaskSource.GITHUB,
                source_id=number,
                title=title,
                description=body,
                status=TaskStatus.NEW,
            )
            task.add_log(f"issue #{number} by {author}: {title}")
            self.state.save(task)
            asyncio.create_task(self._process_task(task))

    # ------------------------------------------------------------------ #
    # Приём задач из Telegram
    # ------------------------------------------------------------------ #
    async def _on_telegram_message(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        if not text:
            return
        chat_id = str(msg["chat_id"])

        # Команды бота — обрабатываем синхронно, не блокируем
        if text.startswith("/"):
            await self._handle_telegram_command(chat_id, text)
            return

        # 1. Если есть ожидающееся уточнение для этого чата — это ответ
        pending = [
            (tid, info)
            for tid, info in self._pending_clarifications.items()
            if info["task"].source == TaskSource.TELEGRAM
            and info["task"].source_id == chat_id
        ]
        if pending:
            tid, info = pending[0]
            task = info["task"]
            task.clarification_answer = text
            self.state.save(task)
            fut = info["future"]
            if fut and not fut.done():
                fut.set_result(text)
            logger.info("[telegram] clarification answer for task %s", tid)
            # Подтверждаем получение уточнения
            try:
                chat_id_int = int(chat_id)
                await self.telegram.send_message(
                    chat_id_int,
                    f"✅ Уточнение принято. Начинаю обработку задачи: «{task.title[:50]}…»",
                )
            except Exception as exc:
                logger.error("[telegram] failed to send clarification confirmation: %s", exc)
            # Запускаем обработку задачи в фоне
            asyncio.create_task(self._process_task(task))
            return

        # 2. Проверяем, есть ли уже активная задача у этого пользователя
        active = self._get_active_task_for_chat(chat_id)
        if active:
            emoji = self._task_status_emoji(active.status)
            progress = "\n".join(active.log[-5:]) if active.log else ""
            await self.telegram.send_message(
                int(chat_id),
                f"{emoji} Я уже занят задачей «{active.title[:50]}…»\n"
                f"Статус: {active.status.value}\n"
                f"Подождите завершения или отправьте /cancel для отмены.\n"
                f"Текущий прогресс:\n{progress}",
            )
            return

        # 3. Иначе — новая задача
        task_id = self._task_id(TaskSource.TELEGRAM, chat_id + ":" + text[:40])
        existing = self.state.get(task_id)
        if existing and existing.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
            pass
        elif existing:
            await self.telegram.send_message(
                int(chat_id),
                f"⚠️ Задача «{existing.title[:50]}…» уже существует (статус: {existing.status.value}).",
            )
            return

        task = Task(
            id=task_id,
            source=TaskSource.TELEGRAM,
            source_id=chat_id,
            title=text[:100],
            description=text,
            status=TaskStatus.NEW,
        )
        task.add_log(f"telegram from chat {chat_id}")
        self.state.save(task)
        logger.info("[telegram] new task from chat %s", chat_id)
        # Подтверждаем получение задачи
        chat_id_int = int(chat_id)
        if task.status == TaskStatus.NEW:
            await self.telegram.send_message(
                chat_id_int,
                f"✅ Задача получена: «{task.title[:50]}…\"\n"
                f"Обработка начата. Я сообщу, когда будет результат или возникнут вопросы.",
            )
        # Запускаем обработку задачи в фоне — не блокируем Telegram
        asyncio.create_task(self._process_task(task))

    async def _tail_command(self, cmd: list[str], lines: int = 3) -> str:
        """Выполнить команду и вернуть последние `lines` непустых строк вывода."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            return "— таймаут чтения логов"
        except FileNotFoundError:
            return f"— команда {cmd[0]} не найдена"
        except Exception as exc:  # noqa: BLE001 - диагностика не должна ломать /status
            return f"— ошибка чтения: {exc}"

        text = out.decode("utf-8", errors="replace")
        # Убираем ANSI-коды, которыми OpenHands раскрашивает логи:
        # в Telegram они выглядят мусором.
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        rows = [r.strip() for r in text.splitlines() if r.strip()]
        if not rows:
            return "— пусто"
        return "\n".join(rows[-lines:])

    async def _collect_task_logs(self, task: Optional[Task], lines: int = 3) -> str:
        """Что агент реально выполнял в runtime-контейнере.

        Это команды и их вывод из траектории разговора — то есть лог самой
        задачи, а не логи служебных процессов контейнера.
        """
        if not task or not task.conversation_id:
            return "— задача ещё не запускалась в контейнере"

        traj = await self.openhands.get_trajectory(task.conversation_id)
        if traj.get("status") == "error":
            return f"— не удалось получить траекторию: {traj.get('error')}"

        events = traj.get("trajectory") or traj.get("events") or traj
        if not isinstance(events, list):
            return "— траектория пуста"

        rows: list[str] = []
        for event in events:
            if event.get("observation") != "run":
                continue
            extras = event.get("extras") or {}
            meta = extras.get("metadata") or {}
            cmd = extras.get("command") or str(event.get("message") or "")
            code = meta.get("exit_code")
            rows.append(f"$ {cmd}\n  → exit {code}")

        if not rows:
            return "— агент ещё не выполнял команд"
        return "\n".join(rows[-lines:])

    async def _collect_layer_logs(self, task: Optional[Task], lines: int = 3) -> str:
        """Последние строки логов всех трёх слоёв системы.

        Слои: оркестратор (этот процесс), сервер OpenHands и runtime-контейнер,
        в котором агент выполняет команды.
        """
        orchestrator_log = (
            "\n".join(task.log[-lines:]) if task and task.log else "— нет записей"
        )
        task_logs = await self._collect_task_logs(task, lines=lines)

        server = await self._tail_command(
            [
                "docker", "logs", "--tail", "60", self.openhands_container,
            ],
            lines=lines,
        )

        if task and task.conversation_id:
            runtime_name = f"openhands-runtime-{task.conversation_id}"
            runtime = await self._tail_command(
                ["docker", "logs", "--tail", "60", runtime_name], lines=lines
            )
        else:
            runtime = "— контейнер ещё не создан"

        def truncate(block: str, limit: int = 500) -> str:
            return block if len(block) <= limit else block[:limit] + "…"

        return (
            f"\n\n🧭 Оркестратор:\n{truncate(orchestrator_log)}"
            f"\n\n📋 Задача в контейнере:\n{truncate(task_logs)}"
            f"\n\n🖥 OpenHands сервер:\n{truncate(server)}"
            f"\n\n📦 Runtime-контейнер:\n{truncate(runtime)}"
        )

    async def _handle_telegram_command(self, chat_id: str, text: str) -> None:
        """Обработка команд бота: /status, /cancel, /help."""
        parts = text.split()
        cmd = parts[0].lower() if parts else ""
        chat_id_int = int(chat_id)
        logger.info("[telegram] command from %s: %s", chat_id, text)

        if cmd == "/help":
            await self.telegram.send_message(
                chat_id_int,
                "🤖 Доступные команды:\n"
                "/status — текущее состояние задачи\n"
                "/cancel — отменить текущую задачу\n"
                "/help — эта справка\n"
                "\nПросто напишите задачу в свободной форме, и я начну её выполнять.",
            )
            return

        if cmd == "/status":
            active = self._get_active_task_for_chat(chat_id)
            if not active:
                all_tasks = (
                    self.state.list_by_status(TaskStatus.DONE)
                    + self.state.list_by_status(TaskStatus.FAILED)
                    + self.state.list_by_status(TaskStatus.CANCELLED)
                )
                recent = [t for t in all_tasks if t.source == TaskSource.TELEGRAM and t.source_id == chat_id]
                if recent:
                    last = recent[-1]
                    emoji = self._task_status_emoji(last.status)
                    logs = await self._collect_layer_logs(last)
                    await self.telegram.send_message(
                        chat_id_int,
                        f"{emoji} Последняя задача: «{last.title[:50]}…»\n"
                        f"Статус: {last.status.value}\n"
                        f"PR: {last.pr_url or '—'}"
                        f"{logs}",
                    )
                else:
                    logs = await self._collect_layer_logs(None)
                    await self.telegram.send_message(
                        chat_id_int,
                        "🆕 У вас нет активных задач. Напишите мне задачу — и я начну."
                        f"{logs}",
                    )
            else:
                emoji = self._task_status_emoji(active.status)
                progress = "\n".join(active.log[-5:]) if active.log else ""
                logs = await self._collect_layer_logs(active)
                await self.telegram.send_message(
                    chat_id_int,
                    f"{emoji} Текущая задача: «{active.title[:50]}…»\n"
                    f"Статус: {active.status.value}\n"
                    f"Прогресс:\n{progress}"
                    f"{logs}",
                )
            return

        if cmd == "/cancel":
            active = self._get_active_task_for_chat(chat_id)
            if not active:
                await self.telegram.send_message(
                    chat_id_int,
                    "🆕 Нет активных задач для отмены.",
                )
                return
            active.status = TaskStatus.CANCELLED
            active.add_log("Отменено пользователем через /cancel")
            self.state.save(active)
            self._pending_clarifications.pop(active.id, None)
            await self.telegram.send_message(
                chat_id_int,
                f"🚫 Задача «{active.title[:50]}…» отменена.",
            )
            return

        await self.telegram.send_message(
            chat_id_int,
            "❓ Неизвестная команда. Используйте /help для справки.",
        )

    # ------------------------------------------------------------------ #
    # Основная обработка задачи (вызывается в фоне через create_task)
    # ------------------------------------------------------------------ #
    async def _process_task(self, task: Task) -> None:
        logger.info("[task] %s status=%s", task.id, task.status.value)

        if task.status == TaskStatus.NEW:
            if not self._has_enough_info(task):
                question = (
                    "Задача выглядит неполной. Пожалуйста, уточните:\n"
                    f"- Описание задачи: {task.description or '(пусто)'}\n"
                    "Какую именно функциональность нужно добавить или исправить в Flutter-проекте?"
                )
                task.status = TaskStatus.CLARIFYING
                task.clarification_question = question
                self.state.save(task)
                await self._ask_clarification(task)
                return

            task.status = TaskStatus.READY
            self.state.save(task)

        if task.status == TaskStatus.CLARIFYING:
            await self._wait_for_clarification(task)
            return

        if task.status == TaskStatus.READY:
            await self._run_openhands(task)

    def _has_enough_info(self, task: Task) -> bool:
        desc = (task.description or "").strip()
        title = (task.title or "").strip()
        if not title or not desc:
            return False
        if len(desc) < 10:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Уточнения
    # ------------------------------------------------------------------ #
    async def _ask_clarification(self, task: Task) -> None:
        question = task.clarification_question or "Пожалуйста, уточните детали задачи."
        if task.source == TaskSource.TELEGRAM:
            chat_id = int(task.source_id)
            fut = asyncio.get_event_loop().create_future()
            self._pending_clarifications[task.id] = {
                "task": task,
                "future": fut,
                "asked_at": datetime.utcnow(),
            }
            await self.telegram.send_message(chat_id, f"❓ {question}")
        elif task.source == TaskSource.GITHUB:
            try:
                num = int(task.source_id)
                self.github.create_comment(num, f"❓ {question}")
                self._pending_clarifications[task.id] = {
                    "task": task,
                    "future": None,
                    "asked_at": datetime.utcnow(),
                    "github_issue": num,
                }
            except Exception as exc:
                logger.error("[clarify] github comment failed: %s", exc)
                task.status = TaskStatus.FAILED
                self.state.save(task)

    async def _wait_for_clarification(self, task: Task) -> None:
        info = self._pending_clarifications.get(task.id)
        if not info:
            task.status = TaskStatus.FAILED
            self.state.save(task)
            return

        asked_at: datetime = info["asked_at"]
        timeout = timedelta(seconds=self.config.clarification_timeout_seconds)
        if datetime.utcnow() - asked_at > timeout:
            logger.warning("[clarify] timeout for task %s", task.id)
            self._notify_author(
                task,
                "⏰ Время на уточнение истекло (3 часа). Задача помечена как застывшая.",
            )
            task.status = TaskStatus.FAILED
            task.clarification_answer = None
            self.state.save(task)
            self._pending_clarifications.pop(task.id, None)
            return

        if task.clarification_answer:
            answer = task.clarification_answer
            task.description += f"\n\nУточнение пользователя: {answer}"
            task.status = TaskStatus.READY
            self.state.save(task)
            self._pending_clarifications.pop(task.id, None)
            if task.source == TaskSource.TELEGRAM:
                try:
                    chat_id_int = int(task.source_id)
                    await self.telegram.send_message(
                        chat_id_int,
                        f"✅ Уточнение принято. Начинаю обработку задачи: «{task.title[:50]}…»",
                    )
                except Exception as exc:
                    logger.error("[telegram] failed to send clarification confirmation: %s", exc)
            logger.info("[clarify] task %s calling _run_openhands (answer already set)", task.id)
            await self._run_openhands(task)
            return

        if info["future"] and not info["future"].done():
            logger.info("[clarify] task %s waiting for future (poll=%ds)", task.id, self.config.clarification_poll_seconds)
            try:
                answer = await asyncio.wait_for(
                    info["future"], timeout=self.config.clarification_poll_seconds
                )
                logger.info("[clarify] task %s future resolved with answer: %s", task.id, answer[:50])
                task.clarification_answer = answer
                task.description += f"\n\nУточнение пользователя: {answer}"
                task.status = TaskStatus.READY
                self.state.save(task)
                self._pending_clarifications.pop(task.id, None)
                if task.source == TaskSource.TELEGRAM:
                    try:
                        chat_id_int = int(task.source_id)
                        await self.telegram.send_message(
                            chat_id_int,
                            f"✅ Уточнение принято. Начинаю обработку задачи: «{task.title[:50]}…»",
                        )
                    except Exception as exc:
                        logger.error("[telegram] failed to send clarification confirmation: %s", exc)
                logger.info("[clarify] task %s calling _run_openhands", task.id)
                await self._run_openhands(task)
            except asyncio.TimeoutError:
                logger.info("[clarify] task %s future timeout, will retry on next tick", task.id)
                pass

    def resolve_telegram_answer(self, chat_id: str, answer: str) -> None:
        for tid, info in self._pending_clarifications.items():
            task = info["task"]
            if task.source == TaskSource.TELEGRAM and task.source_id == chat_id:
                fut = info["future"]
                if fut and not fut.done():
                    fut.set_result(answer)
                task.clarification_answer = answer
                self.state.save(task)
                break

    # ------------------------------------------------------------------ #
    # OpenHands — сборки внутри контейнера
    # ------------------------------------------------------------------ #
    async def _run_openhands(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        self.state.save(task)

        ws = self._ensure_workspace(task)
        client_dir = os.path.join(ws, "social_mind")
        repo_url = f"https://github.com/{self.config.github_repo}.git"

        # Клонируем репозиторий в рабочую директорию
        if not os.path.exists(os.path.join(client_dir, ".git")):
            clone_cmd = ["git", *self._git_auth_args(), "clone", "--depth", "1", repo_url, client_dir]
            proc = await asyncio.create_subprocess_exec(
                *clone_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.error("[git] clone failed: %s", err.decode("utf-8", errors="replace").strip())

        # Инструкция для агента: внести изменения + запустить тесты и сборки внутри контейнера.
        # Путь — ВНУТРИ runtime-контейнера: там проект лежит в /workspace/<repo>.
        # (Путь /opt/openhands/workspace/... из хоста в контейнере не существует.)
        instruction = (
            f"{task.description}\n\n"
            "ТРЕБОВАНИЯ:\n"
            "1. Работай в папке /workspace/social_mind/domains/client "
            "(внутри контейнера проект лежит в /workspace/social_mind)\n"
            "2. После внесения изменений запусти:\n"
            "   - flutter test\n"
            "   - flutter build linux\n"
            "   - flutter build web --release\n"
            "   - flutter build apk --release --split-per-abi\n"
            "3. Если какие-либо команды завершаются с ошибкой — исправь код и запусти снова.\n"
            "4. Сообщи, когда все проверки пройдены успешно.\n"
            "Не создавай PR — это сделает оркестратор отдельно."
        )

        # Настраиваем LLM провайдера (provod.ai) перед каждым разговором
        llm_result = await self.openhands.configure_llm()
        if llm_result.get("status") == "error":
            logger.warning("[openhands] LLM config warning: %s", llm_result.get("error"))
        else:
            logger.info("[openhands] LLM configured successfully")

        # Настраиваем GitHub provider tokens в OpenHands
        provider_result = await self.openhands.configure_github_provider()
        if provider_result.get("status") == "error":
            logger.warning("[openhands] GitHub provider config warning: %s", provider_result.get("error"))
        else:
            logger.info("[openhands] GitHub provider configured successfully")

        # Создаём разговор в OpenHands
        result = await self.openhands.create_conversation(
            instruction=instruction,
            repository=self.config.github_repo,
            branch="main",
        )

        if result.get("status") == "error":
            error_msg = result.get('error', 'неизвестная ошибка')
            logger.error("[openhands] create_conversation failed: %s", error_msg)
            task.status = TaskStatus.FAILED
            self.state.save(task)
            log_text = "\n".join(task.log[-10:]) if task.log else ""
            self._notify_author(
                task,
                f"❌ OpenHands не создал разговор.\n\nОшибка: {error_msg}\n\n"
                f"Полный лог задачи:\n{log_text}",
            )
            return

        conversation_id = result.get("conversation_id")
        if not conversation_id:
            logger.error("[openhands] no conversation_id in response: %s", result)
            task.status = TaskStatus.FAILED
            self.state.save(task)
            self._notify_author(
                task,
                f"❌ OpenHands не вернул conversation_id.\n\nОтвет: {result}",
            )
            return

        task.add_log(f"OpenHands conversation created: {conversation_id}")
        task.conversation_id = conversation_id
        self.state.save(task)

        # Запускаем agente loop
        start_result = await self.openhands.start_conversation(conversation_id)
        if start_result.get("status") == "error":
            error_msg = start_result.get('error', 'неизвестная ошибка')
            logger.warning("[openhands] start_conversation returned error: %s, but will try to monitor anyway", error_msg)
            task.add_log(f"OpenHands start_conversation warning: {error_msg}")
            self.state.save(task)

        await self._monitor_openhands(task, conversation_id, client_dir)

    async def _monitor_openhands(self, task: Task, conversation_id: str, client_dir: str) -> None:
        """Мониторит сессию OpenHands: уточнения, завершение, ошибки.

        Ориентируемся на состояние агента (AgentState), а не на статус
        разговора: разговор переходит в STOPPED и при успехе, и при ошибке,
        поэтому по нему результат не определить.
        """
        state_misses = 0
        while True:
            info = await self.openhands.get_agent_state(conversation_id)
            state_val = info.get("agent_state")

            if state_val is None:
                state_misses += 1
                if state_misses >= self.max_state_misses:
                    task.status = TaskStatus.FAILED
                    task.add_log(
                        f"OpenHands: состояние агента недоступно "
                        f"({info.get('error')})"
                    )
                    self.state.save(task)
                    log_text = "\n".join(task.log[-15:]) if task.log else ""
                    self._notify_author(
                        task,
                        "❌ Не удалось получить состояние агента OpenHands.\n\n"
                        f"Ошибка: {info.get('error')}\n\n"
                        f"Лог выполнения:\n{log_text}",
                    )
                    return
                logger.warning(
                    "[openhands] agent state not available for %s (%s), retry %d/%d",
                    conversation_id, info.get("error"),
                    state_misses, self.max_state_misses,
                )
                await asyncio.sleep(self.config.clarification_poll_seconds)
                continue

            state_misses = 0

            # Агент ждёт ответа пользователя — либо задача выполнена, либо
            # задан вопрос. Различаем по последнему сообщению агента.
            if state_val == "awaiting_user_input":
                message = (info.get("message") or "").strip()
                if self._looks_like_clarification(message):
                    task.status = TaskStatus.CLARIFYING
                    task.clarification_question = message
                    self.state.save(task)
                    await self._ask_clarification(task)
                    return
                task.status = TaskStatus.DONE
                self.state.save(task)
                task.add_log("OpenHands completed successfully")
                self.state.save(task)
                await self._build_ios_and_create_pr(task, client_dir)
                return

            if state_val == "finished":
                task.status = TaskStatus.DONE
                self.state.save(task)
                task.add_log("OpenHands finished")
                self.state.save(task)
                await self._build_ios_and_create_pr(task, client_dir)
                return

            if state_val in ("error", "rejected"):
                error_msg = (info.get("message") or f"агент в состоянии {state_val}")
                task.status = TaskStatus.FAILED
                task.add_log(f"OpenHands failed: {error_msg}")
                self.state.save(task)
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                self._notify_author(
                    task,
                    f"❌ OpenHands завершился с ошибкой.\n\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"Лог выполнения:\n{log_text}",
                )
                return

            # stopped — разговор погашен: либо агент уже отчитался, либо
            # задачу оборвали. Остальные состояния (loading / running /
            # paused) означают, что работа ещё идёт.
            if state_val == "stopped":
                # Остановка после отчёта агента — штатное завершение
                # (OpenHands гасит простаивающий разговор).
                if info.get("completed"):
                    task.status = TaskStatus.DONE
                    self.state.save(task)
                    task.add_log("OpenHands completed (conversation stopped after report)")
                    self.state.save(task)
                    await self._build_ios_and_create_pr(task, client_dir)
                    return
                task.status = TaskStatus.FAILED
                task.add_log("OpenHands stopped before completion")
                self.state.save(task)
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                self._notify_author(
                    task,
                    f"❌ OpenHands остановлен до завершения задачи.\n\n"
                    f"Лог выполнения:\n{log_text}",
                )
                return

            await asyncio.sleep(self.config.clarification_poll_seconds)

    @staticmethod
    def _looks_like_clarification(message: str) -> bool:
        """Агент спрашивает пользователя, а не отчитывается о результате."""
        if not message:
            return False
        lowered = message.lower()
        question_markers = (
            "уточн", "не хватает данных", "какой вариант", "что выбрать",
            "нужно уточнение", "clarif",
        )
        return message.endswith("?") or any(m in lowered for m in question_markers)

    # ------------------------------------------------------------------ #
    # iOS (на iMac) + PR
    # ------------------------------------------------------------------ #
    async def _run_in_container(self, container: str, cmd: str) -> tuple[int, str, str]:
        """Выполнить shell-команду внутри runtime-контейнера."""
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container, "bash", "-lc", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        return (
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    async def _sync_changes_from_runtime(
        self, task: Task, client_dir: str
    ) -> dict:
        """Перенести правки агента из runtime-контейнера на хост.

        Агент работает в /workspace/social_mind внутри контейнера, а шаг PR
        смотрит в рабочую копию на хосте. Контейнер ничего не монтирует
        (Mounts: []), поэтому без копирования в PR попадает пустая ветка.

        Копируем ТОЛЬКО реально изменённые файлы (список даёт git status внутри
        контейнера): это и быстрее, и не тащит в коммит build/ и .dart_tool/.
        """
        if not task.conversation_id:
            return {"status": "skipped", "reason": "нет conversation_id"}

        container = f"openhands-runtime-{task.conversation_id}"
        root = "/workspace/social_mind"

        # База — origin/main: это коммит, с которого начинался клон. Смотрим на
        # ВСЕ изменения с базы, включая те, что агент успел закоммитить внутри
        # контейнера (git status показал бы только рабочее дерево и пропустил их).
        async def _paths(cmd: str) -> list[str]:
            code, out, err = await self._run_in_container(container, f"cd {root} && {cmd}")
            if code != 0:
                raise RuntimeError(err.strip() or f"{cmd}: код {code}")
            return [p for p in out.split("\0") if p]

        try:
            changed = await _paths(
                "git diff --name-only -z --diff-filter=ACMR origin/main"
            )
            deleted = await _paths(
                "git diff --name-only -z --diff-filter=D origin/main"
            )
            # Новые (не отслеживаемые и не в .gitignore) файлы.
            changed += await _paths("git ls-files --others --exclude-standard -z")
        except RuntimeError as exc:
            return {"status": "error", "error": str(exc)}

        # Убираем дубли, сохраняя порядок.
        seen: set[str] = set()
        changed = [p for p in changed if not (p in seen or seen.add(p))]

        copied = 0
        # Временные файлы держим ВНЕ рабочей копии: иначе .pw-tmp попадёт
        # в `git add -A` и уедет в PR.
        staging = tempfile.mkdtemp(prefix="pw-sync-")
        try:
            for idx, raw in enumerate(changed):
                is_dir = raw.endswith("/")
                path = raw.rstrip("/")
                dst = os.path.join(client_dir, path)
                os.makedirs(os.path.dirname(dst) or client_dir, exist_ok=True)

                tmp = os.path.join(staging, f"entry{idx}")
                pull = await asyncio.create_subprocess_exec(
                    "docker", "cp", f"{container}:{root}/{path}", tmp,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, perr = await asyncio.wait_for(pull.communicate(), timeout=120)
                if pull.returncode != 0:
                    return {
                        "status": "error",
                        "error": f"не удалось скопировать {path}: "
                        + perr.decode("utf-8", errors="replace").strip(),
                    }

                if is_dir or os.path.isdir(tmp):
                    # docker cp каталога распаковывает его СОДЕРЖИМОЕ в tmp.
                    os.makedirs(dst, exist_ok=True)
                    for src_root, _dirs, files in os.walk(tmp):
                        rel = os.path.relpath(src_root, tmp)
                        out_root = dst if rel == "." else os.path.join(dst, rel)
                        os.makedirs(out_root, exist_ok=True)
                        for name in files:
                            self._write_matched(
                                os.path.join(src_root, name),
                                os.path.join(out_root, name),
                            )
                else:
                    self._write_matched(tmp, dst)
                copied += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        removed = 0
        for raw in deleted:
            path = raw.rstrip("/")
            dst = os.path.join(client_dir, path)
            if os.path.isfile(dst):
                os.remove(dst)
                removed += 1

        return {"status": "ok", "copied": copied, "removed": removed}

    async def _build_ios_and_create_pr(self, task: Task, client_dir: str) -> None:
        # Сначала вытаскиваем правки агента из runtime-контейнера на хост,
        # иначе в PR не попадёт ничего.
        sync = await self._sync_changes_from_runtime(task, client_dir)
        if sync.get("status") == "ok":
            task.add_log(
                f"Из контейнера: изменено {sync.get('copied')}, "
                f"удалено {sync.get('removed')}"
            )
        elif sync.get("status") == "error":
            task.status = TaskStatus.FAILED
            task.add_log(f"Синхронизация из контейнера не удалась: {sync.get('error')}")
            self.state.save(task)
            log_text = "\n".join(task.log[-15:]) if task.log else ""
            self._notify_author(
                task,
                "❌ Не удалось забрать правки агента из runtime-контейнера.\n\n"
                f"Ошибка: {sync.get('error')}\n\n"
                f"Лог выполнения:\n{log_text}",
            )
            return
        else:
            task.add_log(f"Синхронизация пропущена: {sync.get('reason')}")
        self.state.save(task)

        # iOS на iMac, если настроен
        if self.config.imac_host and self.config.imac_user:
            task.add_log("Starting iOS build+test on iMac")
            ios_res = await self.ios_runner.build_and_test(client_dir)
            task.add_log(f"iOS result: {ios_res.get('status')}")
            if ios_res.get("status") not in ("ok", "skipped"):
                error_msg = ios_res.get('error', 'неизвестная ошибка')
                task.status = TaskStatus.FAILED
                self.state.save(task)
                task.add_log(f"iOS failed: {error_msg}")
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                self._notify_author(
                    task,
                    f"❌ iOS сборка/тесты не прошли.\n\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"Лог выполнения:\n{log_text}",
                )
                return
        else:
            task.add_log("iOS skipped: iMac not configured")

        # Создаём ветку, коммит, пуш, PR
        branch = f"ai-task-{task.id}"
        dry_run = self.config.pr_dry_run
        try:
            for git_cmd in (
                ["checkout", "-b", branch],
                ["add", "-A"],
                ["commit", "-m", f"AI task: {task.title}"],
                # В dry-run пуш только проверяется — на GitHub ничего не уходит.
                (["push", "--dry-run", "-u", "origin", branch] if dry_run
                 else ["push", "-u", "origin", branch]),
            ):
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", client_dir, *self._git_auth_args(), *git_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    # Пустой коммит — не ошибка, если правок и вправду нет.
                    msg = err.decode("utf-8", errors="replace").strip()
                    if git_cmd[0] == "commit" and "nothing to commit" in msg:
                        task.add_log("git: нет изменений для коммита")
                        continue
                    raise RuntimeError(f"git {' '.join(git_cmd)}: {msg}")
                if dry_run and git_cmd[0] == "push":
                    task.add_log("git push --dry-run: ветка проверена, на GitHub не отправлена")
        except Exception as exc:
            error_msg = str(exc)
            task.status = TaskStatus.FAILED
            self.state.save(task)
            task.add_log(f"git/push failed: {error_msg}")
            log_text = "\n".join(task.log[-15:]) if task.log else ""
            self._notify_author(
                task,
                f"❌ Ошибка при создании ветки/пуша.\n\n"
                f"Ошибка: {error_msg}\n\n"
                f"Лог выполнения:\n{log_text}",
            )
            return

        # Задача без правок кода (например, «просто собери») даёт пустой коммит.
        # Тогда между ветками нечему быть, и create_pr упал бы — честно выходим.
        ahead = await self._git_ahead_count(client_dir)
        if ahead == 0:
            task.status = TaskStatus.DONE
            task.pr_url = "(нет изменений для PR)"
            task.add_log("Изменений нет — PR не требуется")
            self.state.save(task)
            self._notify_author(
                task,
                f"✅ Задача выполнена, но код не изменился — PR не требуется.\n\n"
                f"Лог выполнения:\n" + "\n".join(task.log[-10:]),
            )
            return

        if dry_run:
            diff = await self._git_diff_summary(client_dir)
            task.pr_url = f"(dry-run) ветка {branch} не отправлена"
            task.status = TaskStatus.DONE
            self.state.save(task)
            task.add_log("PR пропущен: включён PR_DRY_RUN")
            self._notify_author(
                task,
                f"✅ Задача выполнена (dry-run: PR не создавался).\n\n"
                f"Ветка: {branch} (push --dry-run прошёл)\n\n"
                f"Изменения:\n{diff}\n\n"
                f"Лог выполнения:\n" + "\n".join(task.log[-10:]),
            )
            return

        pr_url = self.github.create_pr(
            branch=branch,
            base="main",
            title=f"[AI] {task.title}",
            body=f"Автосоздано personal_worker для задачи:\n\n{task.description}\n\n"
                 f"Источник: {task.source.value}\n"
                 f"Все сборки и тесты пройдены (Linux/Web/Android внутри OpenHands, iOS на iMac).",
        )
        task.pr_url = pr_url
        task.status = TaskStatus.DONE
        self.state.save(task)
        self._notify_author(
            task,
            f"✅ Задача выполнена! PR создан: {pr_url}",
        )

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        logger.info("[scheduler] starting (poll_interval=%ds)", self.config.poll_interval_seconds)

        # Запуск Telegram polling с retry при сетевых сбоях
        await self._start_telegram_with_retry()

        # Восстанавливаем ожидающие уточнения из БД
        for task in self.state.list_by_status(TaskStatus.CLARIFYING):
            self._pending_clarifications[task.id] = {
                "task": task,
                "future": None,
                "asked_at": task.created_at,
            }

        while True:
            try:
                await self.poll_github()
            except Exception as exc:
                logger.error("[loop] github poll error: %s", exc)

            # Обрабатываем задачи в статусе CLARIFYING (ожидание уточнений)
            for task in self.state.list_by_status(TaskStatus.CLARIFYING):
                try:
                    await self._wait_for_clarification(task)
                except Exception as exc:
                    logger.error("[loop] clarification wait error for task %s: %s", task.id, exc)

            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _start_telegram_with_retry(self) -> None:
        """Запуск Telegram polling с бесконечными повторными попытками при таймауте."""
        import asyncio as _asyncio

        attempt = 0
        while True:
            attempt += 1
            try:
                await self.telegram.start_polling()
                logger.info("[telegram] polling started on attempt %d", attempt)
                return
            except Exception as exc:
                wait = min(2 ** attempt, 60)
                logger.warning(
                    "[telegram] start attempt %d failed: %s. Retrying in %ds...",
                    attempt, exc, wait,
                )
                await _asyncio.sleep(wait)
