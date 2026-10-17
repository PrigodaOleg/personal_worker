"""Планировщик задач: приём, уточнения, OpenHands (сборки внутри контейнера),
iOS на iMac, PR и уведомления.

Жизненный цикл задачи:
  NEW -> CLARIFYING (если не хватает информации) -> READY -> RUNNING (OpenHands)
    -> (OpenHands может вернуть вопрос -> CLARIFYING) -> DONE/FAILED
  После успешного OpenHands: iOS на iMac (по SSH) -> PR в GitHub -> уведомление автору.

Сборки Linux / Web / Android запускаются не на хосте, а внутри контейнера
OpenHands (там же установлен Flutter SDK). Агент сам выполняет
flutter test / flutter build linux / flutter build web / flutter build apk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
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
        )
        self.imac = IMacAdapter(
            host=config.imac_host or "",
            user=config.imac_user or "",
            ssh_key=config.imac_ssh_key,
            work_dir=config.imac_work_dir,
        )
        self.ios_runner = IOSRunner(imac=self.imac)

        self._pending_clarifications: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Вспомогательные
    # ------------------------------------------------------------------ #
    def _task_id(self, source: TaskSource, source_id: str) -> str:
        raw = f"{source.value}:{source_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _workspace_for(self, task: Task) -> str:
        return os.path.join(self.config.work_dir, task.id)

    def _ensure_workspace(self, task: Task) -> str:
        ws = self._workspace_for(task)
        os.makedirs(ws, exist_ok=True)
        return ws

    def _notify_author(self, task: Task, text: str) -> None:
        """Отправить уведомление автору задачи в исходном канале."""
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
                asyncio.create_task(self.telegram.send_message(chat_id, text))
                logger.info("[notify] telegram message sent to %s", chat_id)
            except Exception as exc:
                logger.error("[notify] telegram send failed: %s", exc)

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
            await self._process_task(task)

    # ------------------------------------------------------------------ #
    # Приём задач из Telegram
    # ------------------------------------------------------------------ #
    async def _on_telegram_message(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        if not text:
            return
        chat_id = str(msg["chat_id"])

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
            return

        # 2. Иначе — новая задача
        task_id = self._task_id(TaskSource.TELEGRAM, chat_id + ":" + text[:40])
        if self.state.get(task_id):
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
        await self._process_task(task)

    # ------------------------------------------------------------------ #
    # Основная обработка задачи
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
            await self._run_openhands(task)
            return

        if info["future"] and not info["future"].done():
            try:
                answer = await asyncio.wait_for(
                    info["future"], timeout=self.config.clarification_poll_seconds
                )
                task.clarification_answer = answer
                task.description += f"\n\nУточнение пользователя: {answer}"
                task.status = TaskStatus.READY
                self.state.save(task)
                self._pending_clarifications.pop(task.id, None)
                await self._run_openhands(task)
            except asyncio.TimeoutError:
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

        # Клонируем репозиторий в рабочую директорию (видимую из контейнера OpenHands)
        if not os.path.exists(os.path.join(client_dir, ".git")):
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", repo_url, client_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        # Инструкция для агента: внести изменения + запустить тесты и сборки внутри контейнера
        instruction = (
            f"{task.description}\n\n"
            "ТРЕБОВАНИЯ:\n"
            "1. Работа в папке /opt/openhands/workspace/social_mind/domains/client\n"
            "2. После внесения изменений запусти:\n"
            "   - flutter test\n"
            "   - flutter build linux\n"
            "   - flutter build web --release\n"
            "   - flutter build apk --release --split-per-abi\n"
            "3. Если какие-либо команды завершаются с ошибкой — исправь код и запусти снова.\n"
            "4. Сообщи, когда все проверки пройдены успешно.\n"
            "Не создавай PR — это сделает оркестратор отдельно."
        )

        result = await self.openhands.start_task(
            instruction=instruction,
            work_dir=client_dir,
            repo_url=repo_url,
            branch="main",
        )

        if result.get("status") == "error":
            logger.error("[openhands] start failed: %s", result.get("error"))
            task.status = TaskStatus.FAILED
            self.state.save(task)
            self._notify_author(task, f"❌ OpenHands не запустился: {result.get('error')}")
            return

        session_id = result.get("session_id") or result.get("id") or task.id
        await self._monitor_openhands(task, session_id, client_dir)

    async def _monitor_openhands(self, task: Task, session_id: str, client_dir: str) -> None:
        """Мониторит сессию OpenHands: уточнения, завершение, ошибки."""
        while True:
            status = await self.openhands.get_status(session_id)
            state_val = status.get("status", "unknown")

            if state_val == "awaiting_clarification":
                question = status.get("question", "Нужно уточнение от пользователя.")
                task.status = TaskStatus.CLARIFYING
                task.clarification_question = question
                self.state.save(task)
                await self._ask_clarification(task)
                return

            if state_val in ("completed", "done", "success"):
                task.status = TaskStatus.DONE
                self.state.save(task)
                task.add_log("OpenHands completed successfully")
                await self._build_ios_and_create_pr(task, client_dir)
                return

            if state_val in ("failed", "error"):
                task.status = TaskStatus.FAILED
                self.state.save(task)
                self._notify_author(
                    task,
                    f"❌ OpenHands завершился с ошибкой: {status.get('error', '')}",
                )
                return

            await asyncio.sleep(self.config.clarification_poll_seconds)

    # ------------------------------------------------------------------ #
    # iOS (на iMac) + PR
    # ------------------------------------------------------------------ #
    async def _build_ios_and_create_pr(self, task: Task, client_dir: str) -> None:
        # iOS на iMac, если настроен
        if self.config.imac_host and self.config.imac_user:
            task.add_log("Starting iOS build+test on iMac")
            ios_res = await self.ios_runner.build_and_test(client_dir)
            task.add_log(f"iOS result: {ios_res.get('status')}")
            if ios_res.get("status") not in ("ok", "skipped"):
                task.status = TaskStatus.FAILED
                self.state.save(task)
                self._notify_author(
                    task,
                    f"❌ iOS сборка/тесты не прошли: {ios_res.get('error', '')}",
                )
                return
        else:
            task.add_log("iOS skipped: iMac not configured")

        # Создаём ветку, коммит, пуш, PR
        branch = f"ai-task-{task.id}"
        await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "checkout", "-b", branch,
        )
        await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "add", "-A",
        )
        await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "commit", "-m", f"AI task: {task.title}",
        )
        await asyncio.create_subprocess_exec(
            "git", "-C", client_dir, "push", "origin", branch,
        )

        pr_url = await self.github.create_pr(
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
        await self.telegram.start_polling()

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

            await asyncio.sleep(self.config.poll_interval_seconds)
