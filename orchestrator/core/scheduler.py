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
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from orchestrator.adapters.github import GitHubAdapter
from orchestrator.adapters.openhands import OpenHandsAdapter
from orchestrator.adapters.telegram import MENU_COMMANDS, TelegramAdapter
from orchestrator.core.config import Config
from orchestrator.core import pricing
from orchestrator.core.state import StateStore
from orchestrator.core.task import Task, TaskSource, TaskStatus

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
        self.telegram.set_callback_query_callback(self._on_telegram_callback)
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

        self._pending_clarifications: dict[str, dict] = {}
        # Ожидающие ответа пользователя ВОПРОСЫ АГЕНТА (в процессе работы):
        # task.id -> {task, conversation_id, options, future, asked_at}.
        self._pending_agent_questions: dict[str, dict] = {}
        # Режим «админ отвечает на вопрос агента из Telegram»: chat_id → task_id.
        self._admin_answer_chat: dict[str, str] = {}
        self._pending_notifications: set = set()  # фоновые отправки в Telegram
        self._background_tasks: set = set()  # фоновые asyncio-задачи (держим ссылку)
        self._llm_configured: bool = False
        self._running_tasks: set[str] = set()  # задачи, которые выполняются в фоне
        # Имя контейнера сервера OpenHands — нужно для вывода логов в /status.
        self.openhands_container: str = os.getenv(
            "OPENHANDS_CONTAINER", "openhands-flutter"
        )
        # Сколько опросов подряд терпеть недоступное состояние агента,
        # прежде чем считать задачу упавшей.
        self.max_state_misses: int = int(os.getenv("OPENHANDS_MAX_STATE_MISSES", "10"))
        # Сколько секунд ждать выхода разговора из loading при холодном
        # старте, прежде чем перезапускать/пересоздавать разговор.
        self.loading_startup_timeout_seconds: int = int(
            os.getenv("OPENHANDS_LOADING_TIMEOUT_SECONDS", "600")
        )
        # CI-автоисправление: сколько раундов «логи → агент → допуш»
        # разрешено после провала GHA-сборки.
        self.max_ci_fix_rounds: int = int(os.getenv("GHA_FIX_MAX_ROUNDS", "2"))
        # Сколько секунд ждать нового прогона CI после допуша агента.
        self.ci_new_run_wait_seconds: int = int(
            os.getenv("GHA_NEW_RUN_WAIT_SECONDS", "1800")
        )
        # Сколько раз подряд можно авто-перезапускать прерванную задачу после
        # сбоя/перезагрузки, прежде чем пометить её failed (защита от зацикливания).
        self.max_resume_attempts: int = int(os.getenv("OPENHANDS_MAX_RESUME_ATTEMPTS", "3"))
        # Сколько раз будить разговор, погасший во время ожидания ответа
        # пользователя (OpenHands гасит простаивающие разговоры по idle-таймауту).
        self.max_stop_restarts: int = int(os.getenv("OPENHANDS_MAX_STOP_RESTARTS", "3"))
        # Сколько раз подряд авто-продолжать агента, который встал на паузу без
        # вопроса (объявил намерение и замолчал). Когда лимит исчерпан —
        # вопрос автору задачи.
        self.max_auto_continues: int = int(os.getenv("OPENHANDS_MAX_AUTO_CONTINUES", "4"))
        # Временные ошибки агента (недоступность LLM: 503/429/таймаут/обрыв
        # связи) — не повод хоронить задачу: ждём и продолжаем разговор.
        self.max_error_retries: int = int(os.getenv("OPENHANDS_MAX_ERROR_RETRIES", "5"))
        # Сколько секунд ждать между ретраями временной ошибки — провайдеру
        # LLM нужно время, чтобы подняться.
        self.error_retry_wait_seconds: int = int(
            os.getenv("OPENHANDS_ERROR_RETRY_WAIT_SECONDS", "300")
        )
        # Слежение за сборкой GitHub Actions для финального комментария:
        # период опроса статуса запуска и общий лимит ожидания.
        self.gha_watch_poll_seconds: int = int(os.getenv("GHA_WATCH_POLL_SECONDS", "60"))
        self.gha_watch_timeout_seconds: int = int(os.getenv("GHA_WATCH_TIMEOUT_SECONDS", "5400"))
        # Очередь задач: выполняется строго одна за раз (слабое железо —
        # локальные docker-сборки и один iMac параллельную работу не тянут).
        self._task_queue: "asyncio.Queue[Task]" = asyncio.Queue()
        self._queue_worker_started: bool = False
        # Адаптеры GitHub по репозиториям (мульти-репо): имя → клон self.github.
        self._repo_adapters: dict[str, GitHubAdapter] = {}

    # ------------------------------------------------------------------ #
    # Вспомогательные
    # ------------------------------------------------------------------ #
    def _task_id(self, source: TaskSource, source_id: str) -> str:
        raw = f"{source.value}:{source_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ #
    # Несколько репозиториев
    # ------------------------------------------------------------------ #
    # Ключ настроек со списком репозиториев (JSON-массив owner/name).
    REPOS_SETTING_KEY = "repos"

    def configured_repos(self) -> list[str]:
        """Репозитории, которые опрашиваем на предмет задач.

        Хранятся в settings БД (меняются через Telegram без рестарта).
        Пусто → дефолтный репозиторий из config.yaml.
        """
        raw = self.state.get_setting(self.REPOS_SETTING_KEY)
        if raw:
            try:
                repos = json.loads(raw)
                if isinstance(repos, list) and repos:
                    return [str(r) for r in repos]
            except (ValueError, TypeError):
                logger.warning("[repos] битый список в settings: %r", raw)
        return [self.config.github_repo] if self.config.github_repo else []

    def set_configured_repos(self, repos: list[str]) -> None:
        # Дефолтный репо всегда остаётся в списке: он origin-конфигурации.
        if self.config.github_repo and self.config.github_repo not in repos:
            repos.append(self.config.github_repo)
        self.state.set_setting(self.REPOS_SETTING_KEY, json.dumps(repos))

    def github_for(self, repo: Optional[str]) -> GitHubAdapter:
        """Адаптер для конкретного репозитория задачи.

        None/дефолт → основной адаптер. Иначе — кэшированный клон с тем же
        App; если дефолтная установка не даёт доступ к репо (репозиторий на
        другом аккаунте), подбираем installation из списка установок App.
        """
        default = self.config.github_repo
        if not repo or repo == default:
            return self.github
        cached = self._repo_adapters.get(repo)
        if cached is not None:
            return cached
        base = self.github.for_repo(repo)
        if base.has_repo_access(repo):
            self._repo_adapters[repo] = base
            return base
        # Нет доступа под дефолтной установкой: ищем среди всех установок App.
        for inst in base.list_installations():
            candidate = base.with_installation(inst["id"])
            if candidate.has_repo_access(repo):
                self._repo_adapters[repo] = candidate
                logger.info(
                    "[repos] %s доступен через installation %s (%s)",
                    repo, inst["id"], inst.get("account_login"),
                )
                return candidate
        # Доступа нет нигде — вернём клон с дефолтной установкой: вызывающий
        # код получит честную ошибку 404/403 при обращении к API.
        return base

    def _install_instructions(self, repo: str, slug: Optional[str] = None) -> str:
        """Инструкция админу: что нажать в GitHub, чтобы подключить репозиторий."""
        slug = slug or self.github.app_slug() or "<slug-app>"
        return (
            "Чтобы я работал с этим репозиторием, нужно в GitHub:\n"
            f"1. Открыть https://github.com/apps/{slug}/installations/new\n"
            "   (если App уже установлен на аккаунте — Repository access → "
            "Select repositories → добавить репозиторий).\n"
            f"2. Выбрать репозиторий {repo} и подтвердить.\n"
            f"3. Написать мне /repo check {repo} — я проверю доступ и "
            f"создам метку {self.config.github_label}, если её нет."
        )

    def _repo_full(self, task: Task) -> str:
        """Репозиторий задачи: явный или дефолтный."""
        return task.repo or self.config.github_repo

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

    def _git_auth_args(self, repo: Optional[str] = None) -> list[str]:
        """Аргументы git для аутентификации и авторства коммитов.

        Токен передаётся заголовком, а не в URL: иначе он утекает в
        `git remote -v` и в логи ошибок git. Идентичность задаём явно —
        в контейнере git обычно не настроен, и `git commit` падает с
        "Author identity unknown". Токен установки даёт доступ только к
        её репозиториям, поэтому для не-дефолтного репо берём токен его
        адаптера.
        """
        user, token = self.github_for(repo).get_git_credentials()
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
            async def _comment_to_issue() -> None:
                try:
                    num = int(task.source_id)
                    await asyncio.to_thread(self.github_for(task.repo).create_comment, num, text)
                    logger.info("[notify] comment added to issue #%s", num)
                except Exception as exc:
                    logger.error("[notify] github comment failed: %s", exc)

            self._spawn(_comment_to_issue())
        elif task.source == TaskSource.TELEGRAM:
            try:
                chat_id = int(task.source_id)
                fut = asyncio.ensure_future(self.telegram.send_message(chat_id, text))
                self._pending_notifications.add(fut)
                fut.add_done_callback(self._pending_notifications.discard)
                logger.info("[notify] telegram message queued for %s", chat_id)
            except Exception as exc:
                logger.error("[notify] telegram send failed: %s", exc)

    async def _notify_summary(self, task: Task, summary: str) -> None:
        """Финальное саммари агента отдельным сообщением (до ссылок).

        Для GitHub — комментарий в issue; для Telegram — сообщение автору.
        Заголовок оформляет оркестратор: сам отчёт агента идёт как есть.
        """
        text = (
            "📝 Саммари проделанной работы:\n\n"
            f"{summary}"
        )
        if task.source == TaskSource.GITHUB:
            num = int(task.source_id)
            await asyncio.to_thread(
                self.github_for(task.repo).create_comment, num, text
            )
            logger.info("[summary] comment added to issue #%s", num)
        else:
            self._notify_author(task, text)

    async def _notify_author_with_comment_id(
        self, task: Task, text: str
    ) -> Optional[int]:
        """Как _notify_author, но ждёт отправки и возвращает id комментария.

        Нужен для финального уведомления: id комментария нужен, чтобы позже
        дописать в него прямые ссылки на артефакты GHA. Для Telegram-задач
        возвращает None (редактировать нечего) — текст уходит штатно.
        """
        if task.source != TaskSource.GITHUB:
            self._notify_author(task, text)
            return None
        try:
            num = int(task.source_id)
            cid = await asyncio.to_thread(self.github_for(task.repo).create_comment, num, text)
            logger.info("[notify] comment added to issue #%s", num)
            return cid
        except Exception as exc:
            logger.error("[notify] github comment failed: %s", exc)
            return None

    async def flush_notifications(self) -> None:
        """Дождаться отправки всех отложенных уведомлений."""
        if self._pending_notifications:
            await asyncio.gather(*list(self._pending_notifications), return_exceptions=True)

    def _spawn(self, coro) -> None:
        """Запустить фоновую задачу, удерживая на неё ссылку.

        asyncio.create_task хранит только слабую ссылку: без удержания GC
        может оборвать задачу в середине выполнения.
        """
        fut = asyncio.ensure_future(coro)
        self._background_tasks.add(fut)
        fut.add_done_callback(self._background_tasks.discard)

    def _enqueue_task(self, task: Task) -> None:
        """Поставить задачу в очередь выполнения.

        Задачи выполняются строго по одной (слабое железо: локальные сборки
        в runtime-контейнере и один iMac на всех). Воркер берёт следующую
        задачу, только когда предыдущая завершилась — включая «хвост» после
        возврата _process_task (ожидание уточнения и запуск агента уходят
        в фон, см. _task_queue_worker).
        """
        self._ensure_queue_worker()
        self._task_queue.put_nowait(task)
        logger.info("[queue] task %s enqueued (depth=%d)", task.id, self._task_queue.qsize())

    def _ensure_queue_worker(self) -> None:
        if self._queue_worker_started:
            return
        self._queue_worker_started = True
        self._spawn(self._task_queue_worker())

    async def _task_queue_worker(self) -> None:
        """Единственный воркер: задачи выполняются последовательно.

        Тонкость: _process_task возвращает управление раньше фактического
        конца работы — после подтверждения уточнения сам запуск агента уходит
        в фон (_run_openhands через _spawn) и держит task.id в _running_tasks
        до завершения. Поэтому после возврата _process_task дожидаемся
        освобождения слота, и только потом берём следующую задачу.
        """
        while True:
            task = await self._task_queue.get()
            try:
                logger.info("[queue] task %s started", task.id)
                await self._process_task(task)
            except Exception as exc:  # noqa: BLE001 — воркер не должен умирать
                logger.error("[queue] task %s crashed: %s", task.id, exc)
            while task.id in self._running_tasks:
                await asyncio.sleep(1)
            self._task_queue.task_done()
            logger.info("[queue] task %s finished, slot free", task.id)

    def _cancel_task(self, task: Task, reason: str) -> None:
        """Отменить задачу: статус, висящие вопросы, остановка разговора OpenHands."""
        task.status = TaskStatus.CANCELLED
        task.add_log(reason)
        self.state.save(task)
        self._pending_clarifications.pop(task.id, None)
        qinfo = self._pending_agent_questions.pop(task.id, None)
        if qinfo:
            fut = qinfo.get("future")
            if fut is not None and not fut.done():
                # None = отмена: ответ агенту не отправляем.
                fut.set_result(None)
        if task.conversation_id:
            self._spawn(self._stop_conversation_quietly(task.conversation_id))

    async def _stop_conversation_quietly(self, conversation_id: str) -> None:
        """Best-effort остановка разговора OpenHands (эндпоинт может отсутствовать)."""
        try:
            await self.openhands.stop_conversation(conversation_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[openhands] stop conversation failed: %s", exc)

    def _notify_admin(self, text: str) -> None:
        """Служебное уведомление админу в Telegram (сбой, восстановление)."""
        chat_id = self.config.telegram_admin_chat_id
        if not chat_id:
            logger.debug("[notify] admin chat_id не задан, уведомление пропущено")
            return
        try:
            fut = asyncio.ensure_future(self.telegram.send_message(chat_id, text))
            self._pending_notifications.add(fut)
            fut.add_done_callback(self._pending_notifications.discard)
        except Exception as exc:  # noqa: BLE001
            logger.error("[notify] admin telegram send failed: %s", exc)

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

    def _get_any_active_task(self, exclude_id: Optional[str] = None) -> Optional[Task]:
        """Есть ли активная задача (любого источника), кроме exclude_id.

        Используется при перезапуске: не стартуем новую задачу, пока не
        завершилась другая, — иначе две задачи с разными моделями начнут
        конфликтовать за глобальные настройки LLM в OpenHands.
        """
        active_statuses = [
            TaskStatus.NEW,
            TaskStatus.CLARIFYING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
        ]
        for status in active_statuses:
            for task in self.state.list_by_status(status):
                if task.id != exclude_id:
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

    def _task_status_label(self, status: TaskStatus) -> str:
        return {
            TaskStatus.NEW: "новая",
            TaskStatus.CLARIFYING: "ждёт уточнения",
            TaskStatus.READY: "готова",
            TaskStatus.RUNNING: "выполняется",
            TaskStatus.DONE: "выполнена",
            TaskStatus.FAILED: "ошибка",
            TaskStatus.CANCELLED: "отменена",
        }.get(status, "—")

    # ------------------------------------------------------------------ #
    # Модели LLM
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strip_provider(model: Optional[str]) -> Optional[str]:
        return model.split("/", 1)[-1] if model else None

    def _default_model(self) -> str:
        """Модель по умолчанию (для новых задач). Переопределяется из /model."""
        stored = self.state.get_setting("default_model")
        base = stored or self.config.openhands_llm_model or ""
        return self._strip_provider(base) or ""

    def _set_default_model(self, model: str) -> None:
        self.state.set_setting("default_model", self._strip_provider(model) or model)

    def _effective_model(self, task: Task) -> str:
        """Модель задачи: своя, либо модель по умолчанию."""
        return self._strip_provider(task.model) or self._default_model()

    async def _finalize_cost(self, task: Task) -> None:
        """Посчитать стоимость работы модели (токены × сохранённые цены) в задаче.

        Токены берём из llm_metrics OpenHands, цены — из БД (pricing.py).
        Вызывается при завершении задачи (успех или ошибка, когда агент уже
        поработал), чтобы цена попала в карточку и уведомление.
        """
        if not task.conversation_id:
            return
        try:
            usage = await self.openhands.get_usage(task.conversation_id)
            task.tokens_prompt = usage.get("prompt_tokens", 0)
            task.tokens_completion = usage.get("completion_tokens", 0)
            task.tokens_cache_read = usage.get("cache_read_tokens", 0)
            cost_rub, _ = pricing.compute_cost(
                self.state,
                self._effective_model(task),
                task.tokens_prompt,
                task.tokens_completion,
                task.tokens_cache_read,
            )
            task.cost_rub = cost_rub
            self.state.save(task)
            logger.info(
                "[cost] task %s: %d in / %d out / %d cache → %.2f ₽",
                task.id, task.tokens_prompt, task.tokens_completion,
                task.tokens_cache_read, cost_rub,
            )
        except Exception as exc:  # noqa: BLE001 - стоимость не должна ломать пайплайн
            logger.warning("[cost] finalize failed for %s: %s", task.id, exc)

    async def _fetch_models(self) -> list[str]:
        """Список доступных моделей (с кэшем)."""
        cached = getattr(self, "_models_cache", None)
        if cached is not None:
            return cached
        res = await self.openhands.list_models()
        models = res.get("models") or []
        if models:
            self._models_cache = models
        return models

    @staticmethod
    def _render_instruction(
        description: str,
        imac_item: str,
        tail_items: str,
        secrets_note: str,
    ) -> str:
        """Инструкция агенту из описания задачи и стандартных требований.

        Отдельный метод для тестируемости: запятая после последней строки в
        скобочной конкатенации делает из неё КОРТЕЖ, OpenHands получает вместо
        строки список и отвечает 422 (issue #5). Здесь это исключено типом.
        """
        instruction = (
            f"{description}\n\n"
            "ТРЕБОВАНИЯ:\n"
            "1. Работай в папке /workspace/social_mind/domains/client "
            "(внутри контейнера проект лежит в /workspace/social_mind)\n"
            "2. После внесения изменений запусти:\n"
            "   - flutter test\n"
            "   - flutter build linux\n"
            "   - flutter build web --release\n"
            "   - flutter build apk --release --split-per-abi\n"
            f"{imac_item}"
            f"{tail_items}"
            f"{secrets_note}"
            "Не создавай PR — это сделает оркестратор отдельно.\n\n"
            "ПРАВИЛА ОБЩЕНИЯ:\n"
            "- Промежуточные статусы («теперь пишу тест…», «проверю классы») "
            "отправляй БЕЗ ожидания ответа и сразу продолжай работу: оркестратор "
            "не передаёт их пользователю, а пустые паузы тормозят задачу.\n"
            "- Останавливаться и ждать ответа пользователя можно ТОЛЬКО задавая "
            "реальный вопрос со знаком вопроса, без которого продолжить нельзя.\n"
            "- Финальный отчёт — только когда ВСЕ пункты требований выполнены "
            "(тесты и все сборки прогнаны); в отчёте перечисли сделанные изменения."
        )
        assert isinstance(instruction, str), (
            "инструкция должна быть строкой, а не кортежем"
        )
        return instruction

    @staticmethod
    def _model_version_key(model_id: str) -> tuple:
        nums = tuple(int(x) for x in re.findall(r"\d+", model_id))
        return (nums, model_id)

    _LITE_SEGMENTS = {
        "flash", "flashx", "mini", "nano", "lite", "haiku", "air", "turbo", "swift",
    }

    def _tier_score(self, model_id: str) -> int:
        """Лёгкие/быстрые модели (flash/mini/lite) ранжируем ниже «полных».

        Иначе новейший `qwen3.8-flash` вытеснил бы более сильный `qwen3.7-max`.
        Сравниваем по целым сегментам: иначе бренды вроде «ge**mini**» и
        «**mini**max» ошибочно попадут в «лёгкие».
        """
        segs = set(re.split(r"[-_.]", model_id.lower()))
        return 0 if segs & self._LITE_SEGMENTS else 1

    def _shortlist_models(self, models: list[str], current: str, default: str) -> list[str]:
        """Сильнейшая/новейшая модель каждого семейства + текущая + по умолчанию.

        Датированные снапшоты (вида `deepseek-v4-pro-0813`) из шорт-листа
        исключаем — это не самостоятельные модели, а точки выгрузки.
        """
        base = [m for m in models if not re.search(r"-\d{4}$", m)]
        families: dict[str, list[str]] = {}
        for m in base:
            fam = re.split(r"[-_.]", m)[0]
            families.setdefault(fam, []).append(m)
        short: list[str] = []
        for lst in families.values():
            best = max(lst, key=lambda m: (self._tier_score(m), self._model_version_key(m)))
            short.append(best)
        for extra in (current, default):
            if extra and extra in models and extra not in short:
                short.append(extra)
        return sorted(set(short))

    # ------------------------------------------------------------------ #
    # Статусы слоёв (для /status)
    # ------------------------------------------------------------------ #
    async def _container_state(self, name: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format",
            "{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return "absent"
        return out.decode("utf-8", errors="replace").strip() or "unknown"

    @staticmethod
    def _state_emoji(state: str) -> str:
        if state in ("running", "healthy", "running/healthy"):
            return "🟢"
        if state == "absent":
            return "⚪"
        return "🔴"

    async def _layer_status_block(self, task: Optional[Task]) -> str:
        """Короткий статус трёх слоёв — без дампов логов."""
        server = await self._container_state(self.openhands_container)
        if task and task.conversation_id:
            runtime = await self._container_state(
                f"openhands-runtime-{task.conversation_id}"
            )
            runtime_line = f"{self._state_emoji(runtime)} runtime ({runtime})"
        else:
            runtime_line = "⚪ runtime (не создан)"
        return (
            f"{self._state_emoji('running')} оркестратор\n"
            f"{self._state_emoji(server)} OpenHands ({server})\n"
            f"{runtime_line}"
        )

    def _model_picker(
        self, target: str, page: int, mode: str, current: str
    ) -> tuple[str, list[list[tuple[str, str]]]]:
        """Текст и кнопки выбора модели. target — 'default' или id задачи."""
        models = getattr(self, "_models_cache", None) or []
        default = self._default_model()
        if mode == "a":
            items = list(models)
            title = "Все модели"
        else:
            items = self._shortlist_models(models, current, default)
            title = "Рекомендуемые модели"

        per_page = 8
        pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        chunk = items[page * per_page:(page + 1) * per_page]

        rows: list[list[tuple[str, str]]] = []
        for m in chunk:
            mark = "✅ " if m == current else ""
            ix, _swe, _avg = pricing.price_quality_index(self.state, m)
            rows.append([(f"{mark}{m} · IX {pricing.fmt_ix(ix)}",
                          f"ms:{target}:{page}:{mode}:{m}")])

        # Навигация
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("⬅️", f"mp:{target}:{page - 1}:{mode}"))
        nav.append(("📄 {}/{}".format(page + 1, pages), "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"mp:{target}:{page + 1}:{mode}"))
        rows.append(nav)

        # Переключатель шорт-лист / все + отмена
        toggle = "📋 Все модели" if mode == "s" else "⭐ Рекомендуемые"
        new_mode = "a" if mode == "s" else "s"
        rows.append([
            (toggle, f"mp:{target}:0:{new_mode}"),
            ("🔙 Назад", f"back:{target}"),
        ])

        label = "по умолчанию" if target == "default" else f"задачи {target[:8]}"
        text = (
            f"🧠 Модель {label}:\n"
            f"Текущая: {current or '—'}\n"
            f"Выберите из списка ({title}):\n\n"
            f"IX — индекс цена/качество: SWE-bench% ÷ ср. цена "
            f"((вход×3+выход)/4, ₽/1M). Выше = лучше.\n\n"
            f"⚠️ Если задача падает сразу — модель отклоняет параметры "
            f"OpenHands, выберите другую."
        )
        return text, rows


    # ------------------------------------------------------------------ #
    # Приём задач из GitHub
    # ------------------------------------------------------------------ #
    async def poll_github(self) -> None:
        logger.debug("[poll] github")
        # Закрытый/удалённый issue = отмена задачи (у GitHub-задач нет /cancel).
        # Первым делом — чтобы не опрашивать комментарии уже мёртвых issue.
        self._cancel_tasks_for_closed_issues()
        # Ответы на уточнения и вопросы агента: автор отвечает комментарием к issue.
        self._poll_github_clarification_answers()
        self._poll_github_agent_question_answers()
        # Ревью PR завершённых задач → агенту (с внутренним троттлингом).
        self._poll_pr_review_feedback()
        for repo in self.configured_repos():
            try:
                issues = self.github_for(repo).fetch_open_issues()
            except Exception as exc:
                logger.error("[poll] github fetch failed for %s: %s", repo, exc)
                continue

            for issue in issues:
                number = str(issue["number"])
                # Дефолтный репо сохраняет старую схему id (совместимость),
                # остальные — с префиксом, чтобы номера не конфликтовали.
                if repo == self.config.github_repo:
                    task_id = self._task_id(TaskSource.GITHUB, number)
                else:
                    task_id = self._task_id(
                        TaskSource.GITHUB, f"{repo}:{number}"
                    )
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
                    repo=repo,
                )
                task.add_log(f"issue #{number} by {author} in {repo}: {title}")
                self.state.save(task)
                self._notify_author(
                    task,
                    f"🚀 Задача принята в работу: «{title[:60]}»\n"
                    "Если понадобятся уточнения — спрошу здесь, в комментариях.",
                )
                self._notify_admin(
                    f"🆕 Новый issue с меткой {self.config.github_label} в {repo}: "
                    f"«{title[:80]}» (автор: {author or 'unknown'})\n"
                    f"{issue.get('url') or ''}".rstrip()
                )
                self._enqueue_task(task)

    def _poll_github_clarification_answers(self) -> None:
        """Забрать ответы на уточнения из комментариев GitHub issues.

        Автор отвечает комментарием к issue: берём первый комментарий
        живого участника после нашего вопроса. Здесь только сбор ответа —
        сама обработка идёт в _wait_for_clarification (основной цикл).
        """
        for task_id, info in list(self._pending_clarifications.items()):
            issue_number = info.get("github_issue")
            task = info.get("task")
            if issue_number is None or task is None or task.clarification_answer:
                continue
            try:
                comments = self.github_for(task.repo).fetch_issue_comments(
                    issue_number,
                    after_comment_id=info.get("github_after_comment_id"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[clarify] github comments fetch failed for #%s: %s",
                    issue_number, exc,
                )
                continue
            for comment in comments:
                body = (comment.get("body") or "").strip()
                if not body:
                    continue
                task.clarification_answer = body
                self.state.save(task)
                logger.info(
                    "[clarify] github answer for task %s from %s: %s",
                    task_id, comment.get("author"), body[:60],
                )
                break

    def _poll_github_agent_question_answers(self) -> None:
        """Ответы на вопросы агента (по ходу работы) из комментариев issue.

        Ответ — первый комментарий живого участника после вопроса: либо
        номер варианта (1..N), либо свободный текст.
        """
        for task_id, qinfo in list(self._pending_agent_questions.items()):
            issue_number = qinfo.get("github_issue")
            fut = qinfo.get("future")
            if issue_number is None or fut is None or fut.done():
                continue
            try:
                comments = self.github_for(qinfo["task"].repo).fetch_issue_comments(
                    issue_number,
                    after_comment_id=qinfo.get("github_after_comment_id"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[agent-question] github comments fetch failed for #%s: %s",
                    issue_number, exc,
                )
                continue
            for comment in comments:
                body = (comment.get("body") or "").strip()
                if not body:
                    continue
                answer = self._match_option(body, qinfo.get("options") or [])
                fut.set_result(answer)
                logger.info(
                    "[agent-question] github answer for task %s from %s: %s",
                    task_id, comment.get("author"), answer[:60],
                )
                break

    @staticmethod
    def _match_option(body: str, options: list[str]) -> str:
        """Номер варианта (1..N) -> сам вариант; иначе свободный текст."""
        stripped = body.strip()
        if stripped.isdigit() and options:
            idx = int(stripped) - 1
            if 0 <= idx < len(options):
                return options[idx]
        return stripped

    # ------------------------------------------------------------------ #
    # Ревью PR → агенту: комментарии/ревью/инлайн-замечания по изменениям
    # ------------------------------------------------------------------ #

    _REVIEW_FEEDBACK_INTERVAL = 120  # опрашивать PR не чаще, чем раз в 2 мин

    def _poll_pr_review_feedback(self) -> None:
        """Новые ревью-комментарии по PR завершённых задач → агенту.

        Задача в DONE, но жизнь PR продолжается: ревьюер оставляет
        замечания. Их получает агент (в тот же разговор), он правит код в
        контейнере, оркестратор допушивает в ветку PR. Состояние опроса —
        «после какого комментария» — в settings БД (переживает рестарт).
        """
        now = time.time()
        last_run = float(
            self.state.get_setting("pr_review_last_poll", "0") or 0
        )
        if now - last_run < float(
            getattr(self, "review_poll_min_interval", self._REVIEW_FEEDBACK_INTERVAL)
        ):
            return
        self.state.set_setting("pr_review_last_poll", str(now))
        for task in self.state.list_by_status(TaskStatus.DONE):
            if task.status != TaskStatus.DONE or not task.pr_url:
                continue
            if "/pull/" not in (task.pr_url or ""):
                continue  # dry-run или «нет изменений»
            key = f"pr_review_last_id:{task.id}"
            after = int(self.state.get_setting(key, "0") or 0)
            try:
                feedback = self.github_for(task.repo).fetch_pr_feedback(
                    task.pr_url, after_comment_id=after
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[pr-review] fetch failed for %s: %s", task.id, exc
                )
                continue
            if not feedback:
                continue
            max_id = max(f["id"] for f in feedback)
            self.state.set_setting(key, str(max_id))
            self._spawn(self._handle_pr_feedback(task, feedback))

    async def _handle_pr_feedback(self, task: Task, feedback: list[dict]) -> None:
        """Переслать замечания ревью агенту и допушить исправления."""
        parts = []
        for f in feedback:
            head = f"--- {f['kind']} от @{f.get('author') or 'unknown'}"
            if f.get("path"):
                head += f" ({f['path']})"
            parts.append(f"{head} ---\n{(f.get('body') or '').strip()[:2000]}")
        message = (
            "💬 По твоему PR появились комментарии ревью. Ниже — замечания "
            "людей к твоим изменениям.\n\n"
            "Разберись в каждом замечании и внеси правки в своём рабочем "
            "окружении (/workspace/social_mind). ПУШИТЬ НЕ НУЖНО: просто "
            "исправь код и сообщи, что готово — оркестратор сам допушит в "
            "ветку PR. Если замечание кажется спорным — объясни свою "
            "позицию текстом, я передам его ревьюеру. Если замечания "
            "требуют решения владельца — задай вопрос со знаком вопроса."
            "\n\n" + "\n\n".join(parts)
        )
        task.add_log(
            f"Ревью PR: {len(feedback)} новых комментариев — отправляю агенту"
        )
        self.state.save(task)
        conversation_id = task.conversation_id
        if not conversation_id:
            logger.warning(
                "[pr-review] нет conversation_id у задачи %s", task.id
            )
            return
        res = await self.openhands.send_message(conversation_id, message)
        if res.get("status") == "error" or res.get("success") is False:
            # Разговор погас после завершения задачи — будим и повторяем.
            await self.openhands.start_conversation(conversation_id)
            res = await self.openhands.send_message(conversation_id, message)
            if res.get("status") == "error" or res.get("success") is False:
                logger.error(
                    "[pr-review] не удалось доставить ревью агенту: %s", res
                )
                self._notify_author(
                    task,
                    "💬 По твоему PR есть новые комментарии ревью, но "
                    "передать их агенту не удалось (разговор закрыт?). "
                    "Разберись вручную или перезапусти задачу.",
                )
                return
        # Агент получил замечания — ждём правки и допушиваем.
        branch = self.github_for(task.repo).fetch_pr_head_branch(task.pr_url)
        if not branch:
            logger.error(
                "[pr-review] не удалось получить ветку PR для %s", task.id
            )
            return
        client_dir = os.path.join(self._ensure_workspace(task), "social_mind")
        new_run = await self._sync_and_push_ci_fix(
            task, client_dir, branch, old_sha=None
        )
        if new_run:
            task.add_log(
                f"Правки по ревью запушены — прогон CI: "
                f"{new_run.get('html_url') or new_run.get('id')}"
            )
            self.state.save(task)
            self._spawn(self._watch_gha_artifacts(
                task,
                comment_id=self.github_for(task.repo).last_bot_comment_id(
                    int(task.source_id)
                ) or 0,
                comment_body="",
                run_id=new_run["id"],
                client_dir=client_dir,
                branch=branch,
            ))
        else:
            self._notify_author(
                task,
                "💬 Комментарии ревью отправлены агенту, но правок он не "
                "внёс (или допуш не удался). Возможно, агент ответил "
                "пояснением — посмотри диалог в задаче.",
            )

    def _cancel_tasks_for_closed_issues(self) -> None:
        """Закрытый issue = отмена задачи: автор закрыл issue — значит, не нужно.

        Отдельной отмены у GitHub-задач нет (Telegram /cancel их не видит),
        поэтому закрытие issue — естественный жест отмены.
        """
        active_statuses = (
            TaskStatus.NEW,
            TaskStatus.CLARIFYING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
        )
        for status in active_statuses:
            for task in self.state.list_by_status(status):
                if task.source != TaskSource.GITHUB:
                    continue
                try:
                    issue_state = self.github_for(task.repo).fetch_issue_state(int(task.source_id))
                except Exception as exc:  # noqa: BLE001
                    if self._issue_gone(exc):
                        # Issue удалён (или стал недоступен) — задача-сирота.
                        self._cancel_task(task, "issue удалён — задача отменена")
                        logger.info(
                            "[task] %s cancelled: issue #%s gone",
                            task.id, task.source_id,
                        )
                        continue
                    logger.error(
                        "[poll] issue state fetch failed for #%s: %s",
                        task.source_id, exc,
                    )
                    continue
                if issue_state != "closed":
                    continue
                self._cancel_task(task, "issue закрыт автором — задача отменена")
                logger.info(
                    "[task] %s cancelled: issue #%s closed", task.id, task.source_id
                )

    @staticmethod
    def _issue_gone(exc: Exception) -> bool:
        """404/410 от GitHub — issue удалён или недоступен."""
        return getattr(exc, "status", None) in (404, 410)

    # ------------------------------------------------------------------ #
    # Приём задач из Telegram
    # ------------------------------------------------------------------ #
    async def _on_telegram_message(self, msg: dict) -> None:
        text = msg.get("text", "").strip()
        if not text:
            return
        chat_id = str(msg["chat_id"])

        # Кнопки постоянного меню превращаем в команды (текст кнопки = команда).
        mapped = MENU_COMMANDS.get(text)
        if mapped:
            text = mapped

        # Команды бота — обрабатываем синхронно, не блокируем
        if text.startswith("/"):
            await self._handle_telegram_command(chat_id, text, username=msg.get("username"))
            return

        # Ответ админа на вопрос агента (режим после кнопки «✍️ Ответить»)
        answer_task_id = self._admin_answer_chat.get(chat_id)
        if answer_task_id:
            self._admin_answer_chat.pop(chat_id, None)
            if text.strip() == "×":
                await self.telegram.send_message(int(chat_id), "↩️ Ответ отменён.")
                return
            qinfo = self._pending_agent_questions.get(answer_task_id)
            fut = (qinfo or {}).get("future")
            if fut and not fut.done():
                fut.set_result(text)
                await self.telegram.send_message(int(chat_id), "✅ Ответ отправлен агенту.")
            else:
                await self.telegram.send_message(
                    int(chat_id), "⌛ Вопрос уже закрыт, ответ не потребовался."
                )
            return

        # Ответ на вопрос АГЕНТА (задан в процессе работы) — отправляем ему.
        for _tid, qinfo in self._pending_agent_questions.items():
            qtask = qinfo["task"]
            if qtask.source == TaskSource.TELEGRAM and qtask.source_id == chat_id:
                fut = qinfo["future"]
                if fut and not fut.done():
                    fut.set_result(text)
                logger.info("[agent-question] answer from chat %s: %s", chat_id, text[:50])
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
            self._enqueue_task(task)
            return

        # 2. Активная задача не мешает приёму: новая встаёт в очередь
        # (задачи выполняются строго по одной — слабое железо).
        active = self._get_active_task_for_chat(chat_id)
        if active:
            emoji = self._task_status_emoji(active.status)
            progress = "\n".join(active.log[-3:]) if active.log else ""
            await self.telegram.send_message(
                int(chat_id),
                f"{emoji} Сейчас выполняется: «{active.title[:50]}…» "
                f"(статус: {active.status.value}).\n"
                f"Новая задача встанет в очередь и начнётся автоматически.\n"
                f"Текущий прогресс:\n{progress}",
            )

        # 3. Новая задача (в очередь — даже если другая ещё выполняется)
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
            position = self._task_queue.qsize() + 1
            queue_note = (
                f"Позиция в очереди: {position}. " if position > 1 else ""
            )
            await self.telegram.send_message(
                chat_id_int,
                f"✅ Задача получена: «{task.title[:50]}…\"\n"
                f"{queue_note}Я сообщу, когда начну и когда будет результат или возникнут вопросы.",
            )
        # В очередь — задачи выполняются строго по одной
        self._enqueue_task(task)

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
        """Что агент реально делает: реплики и команды.

        Из событий разговора (`/events`): команды с кодами выхода ($ ...),
        текущая выполняющаяся команда (🔄) и текстовые реплики агента (💬) —
        «Проверю workflows…», «Инфраструктура ясна, создаю тест…». Без реплик
        по одним командам непонятно, ЗАЧЕМ агент их выполняет. Служебные
        сообщения (Running command, I am thinking) и псевдо-вызовы
        инструментов в реплики не попадают.
        """
        if not task or not task.conversation_id:
            return "— задача ещё не запускалась в контейнере"

        events = await self.openhands.get_events(task.conversation_id)
        if not isinstance(events, list) or not events:
            return "— агент ещё не выполнял команд"

        done: list[str] = []
        running: Optional[str] = None
        for event in events:
            if not isinstance(event, dict):
                continue
            args = event.get("args") or {}
            extras = event.get("extras") or {}
            meta = extras.get("metadata") or {}
            msg = str(event.get("message") or "")
            action = event.get("action")
            obs = event.get("observation")

            if action == "run" and obs is None:
                # команда стартовала, но ещё не завершилась
                cmd = (args.get("command") or extras.get("command")
                       or re.sub(r"^Running command:\s*", "", msg))
                running = self._short_cmd(cmd) or running
            elif obs == "run":
                # команда завершилась
                cmd = extras.get("command") or args.get("command")
                code = meta.get("exit_code")
                if not cmd:
                    m = re.search(r"Command `(.*?)`", msg)
                    cmd = m.group(1) if m else msg
                if code is None:
                    m = re.search(r"exit code (-?\d+)", msg)
                    code = m.group(1) if m else "?"
                cmd = (cmd or "").strip()
                if cmd:
                    done.append(f"$ {self._short_cmd(cmd)}\n  → exit {code}")
                running = None
            elif (
                (action == "message" or (action is None and obs is None))
                and event.get("source") == "agent"
            ):
                # Текстовая реплика агента — что он собирается делать и почему.
                # В API OpenHands реплика приходит как action="message"; события
                # без action оставлены для совместимости со старыми тестами.
                cleaned = self._strip_tool_markup(msg).strip()
                if cleaned and not self._AGENT_NOISE_RE.match(cleaned):
                    done.append(f"💬 {self._short_cmd(cleaned, max_len=160)}")

        rows = list(done[-lines:])
        if running:
            rows.append(f"🔄 {running} — выполняется…")
        if not rows:
            return "— агент ещё не выполнял команд и не писал реплик"
        return "\n".join(rows)

    @staticmethod
    def _short_cmd(cmd: str, max_len: int = 100) -> str:
        cmd = (cmd or "").strip().replace("\n", " ⏎ ")
        return cmd if len(cmd) <= max_len else cmd[: max_len - 1] + "…"

    async def _handle_telegram_command(
        self, chat_id: str, text: str, username: Optional[str] = None
    ) -> None:
        """Обработка команд бота: /status, /cancel, /help."""
        parts = text.split()
        cmd = parts[0].lower() if parts else ""
        chat_id_int = int(chat_id)
        logger.info("[telegram] command from %s: %s", chat_id, text)

        if cmd == "/start":
            await self.telegram.send_message(
                chat_id_int,
                "🤖 Личный помощник по Flutter-задачам.\n\n"
                "Просто напишите задачу — и я начну её выполнять.\n"
                "Модель по умолчанию: "
                f"{self._default_model()}\n\n"
                "Используйте меню внизу:",
                keyboard=self.telegram.build_menu_keyboard(),
            )
            return

        if cmd == "/help":
            await self.telegram.send_message(
                chat_id_int,
                "🤖 Доступные команды (или кнопки меню):\n"
                "📋 Задачи — все задачи, результаты и ссылки на PR; отмена и «подтолкнуть» — кнопками в карточке\n"
                "🧠 Модель — модель LLM по умолчанию\n"
                "🗂 Репозитории — какие репозитории опрашиваю на задачи\n"
                "/repo add|check|del <owner/name> — подключить/проверить/убрать репозиторий (админ)\n"
                "🚫 Отмена — отменить текущую задачу\n"
                "❓ Помощь — эта справка\n"
                "\nПросто напишите задачу в свободной форме — и я начну её выполнять.",
            )
            return

        if cmd == "/status":
            active = self._get_active_task_for_chat(chat_id)
            task = active or (self.state.list_for_chat(chat_id, limit=1) or [None])[0]
            text, keyboard = await self._render_status_view(task)
            await self.telegram.send_message(chat_id_int, text, keyboard=keyboard)
            return

        if cmd == "/tasks":
            await self._send_task_list(chat_id_int, chat_id, page=0)
            return

        if cmd == "/model":
            current = self._default_model()
            text, rows = self._model_picker("default", 0, "s", current)
            await self.telegram.send_message(chat_id_int, text, keyboard=self.telegram.build_keyboard(rows))
            return

        if cmd == "/nudge":
            await self._handle_nudge_command(chat_id)
            return

        if cmd == "/cancel":
            active = self._get_active_task_for_chat(chat_id)
            if not active:
                await self.telegram.send_message(
                    chat_id_int,
                    "🆕 Нет активных задач для отмены.",
                )
                return
            self._cancel_task(active, "Отменено пользователем через /cancel")
            await self.telegram.send_message(
                chat_id_int,
                f"🚫 Задача «{active.title[:50]}…» отменена.",
            )
            return

        if cmd == "/repos":
            await self._handle_repos_command(chat_id_int)
            return

        if cmd in ("/repo", "/repo_add", "/repo_check", "/repo_del"):
            await self._handle_repo_command(chat_id_int, cmd, parts, username)
            return

        await self.telegram.send_message(
            chat_id_int,
            "❓ Неизвестная команда. Используйте /help для справки.",
        )

    # ------------------------------------------------------------------ #
    # Управление репозиториями (только админ)
    # ------------------------------------------------------------------ #
    def _is_admin(self, username: Optional[str]) -> bool:
        admin = self.config.telegram_admin_username
        return bool(admin) and (username or "").lstrip("@").lower() == admin.lstrip("@").lower()

    async def _handle_repos_command(self, chat_id_int: int) -> None:
        repos = self.configured_repos()
        default = self.config.github_repo
        lines = ["🗂 Репозитории, которые я опрашиваю на задачи:"]
        for r in repos:
            marker = " (дефолт из config.yaml)" if r == default else ""
            lines.append(f"  • {r}{marker}")
        lines.append("")
        lines.append(
            "Добавить: /repo add <owner/name>\n"
            "Проверить доступ и метку: /repo check <owner/name>\n"
            "Убрать: /repo del <owner/name>"
        )
        await self.telegram.send_message(chat_id_int, "\n".join(lines))

    async def _handle_repo_command(
        self, chat_id_int: int, cmd: str, parts: list[str], username: Optional[str]
    ) -> None:
        if not self._is_admin(username):
            await self.telegram.send_message(
                chat_id_int, "🔒 Управление репозиториями доступно только админу."
            )
            return
        name = parts[1] if len(parts) > 1 else ""
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
            await self.telegram.send_message(
                chat_id_int,
                "Формат: <owner>/<name>, например /repo add PrigodaOleg/social_mind",
            )
            return

        repos = [r for r in self.configured_repos() if r != self.config.github_repo]

        if cmd == "/repo_del":
            if name not in repos:
                await self.telegram.send_message(
                    chat_id_int, f"ℹ️ {name} и так не в списке (в списке: {repos})."
                )
                return
            repos.remove(name)
            self.set_configured_repos(repos)
            await self.telegram.send_message(
                chat_id_int,
                f"🗑 {name} убран из опроса. Задачи из него, созданные ранее, "
                "не тронуты — доделаются. Опять подключить: /repo add " + name,
            )
            return

        # add и check: проверяем доступ; при отсутствии — инструкция.
        if cmd == "/repo_add" and name in repos:
            await self.telegram.send_message(
                chat_id_int, f"ℹ️ {name} уже в списке. Проверить доступ: /repo check {name}"
            )
            return
        await self.telegram.send_message(chat_id_int, f"⏳ Проверяю доступ к {name}…")
        ok, message = await asyncio.to_thread(self._check_repo_access, name)
        if not ok:
            slug = await asyncio.to_thread(self.github.app_slug)
            await self.telegram.send_message(
                chat_id_int, message + "\n\n" + self._install_instructions(name, slug)
            )
            return
        if cmd in ("/repo_add", "/repo_check") and name != self.config.github_repo:
            if name not in repos:
                repos.append(name)
                self.set_configured_repos(repos)
            await self.telegram.send_message(chat_id_int, message)
            return
        await self.telegram.send_message(chat_id_int, message)

    def _check_repo_access(self, repo: str) -> tuple[bool, str]:
        """Проверить доступ App к репо и наличие метки. Блокирующий (поток)."""
        gh = self.github_for(repo)
        if not gh.has_repo_access(repo):
            return False, f"❌ У приложения GitHub нет доступа к {repo}."
        label = gh.label
        if gh.ensure_label(repo):
            return True, (
                f"✅ {repo} подключен, доступ есть.\n"
                f"Метка {label}: есть. Создавайте issue с меткой {label} — "
                "задача придёт на ближайшем опросе (раз в минуту)."
            )
        return False, (
            f"⚠️ Доступ к {repo} есть, но создать метку {label} не удалось "
            "(проверьте права установки)."
        )

    # ------------------------------------------------------------------ #
    # Telegram UI: список задач, карточка, кнопки, перезапуск
    # ------------------------------------------------------------------ #
    async def _reply(self, query: dict, text: str, keyboard=None) -> None:
        """Ответить на нажатие кнопки: отредактировать сообщение, либо отправить."""
        chat_id = query.get("chat_id")
        message_id = query.get("message_id")
        if chat_id and message_id:
            await self.telegram.edit_message(chat_id, message_id, text, keyboard)
        elif chat_id:
            await self.telegram.send_message(chat_id, text, keyboard)

    def _task_actions_keyboard(self, task: Task):
        rows: list[list[tuple[str, str]]] = []
        # Активную задачу можно отменить прямо из карточки.
        if task.status in (
            TaskStatus.NEW,
            TaskStatus.CLARIFYING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
        ):
            rows.append([("🚫 Отменить", f"cancel:{task.id}")])
        # Висящий вопрос агента: ответить можно прямо отсюда.
        if self._has_pending_question(task.id):
            rows.append([("✍️ Ответить на вопрос", f"qask:{task.id}")])
        # Упавшая после работы агента задача: подтолкнуть дешевле, чем перезапускать.
        if self._can_nudge_finalization(task):
            rows.append([("📤 Подтолкнуть", f"nudge:{task.id}")])
        rows += [
            [("🔄 Обновить", f"refresh:{task.id}")],
            [("🔁 Перезапустить", f"restart:{task.id}"),
             ("🧠 Модель", f"model:{task.id}")],
            [("📖 Логи", f"logs:{task.id}"),
             ("📋 К списку", "tasks:0")],
            [("🗑 Удалить", f"delask:{task.id}")],
        ]
        return self.telegram.build_keyboard(rows)

    def _source_tag(self, task: Task) -> str:
        return "🐙" if task.source == TaskSource.GITHUB else "💬"

    async def _render_status_view(self, task: Optional[Task]) -> tuple[str, object]:
        """Полный статус задачи (слой/логи/цена) + клавиша «Обновить».

        Общий рендер для /status и кнопки 🔄 Обновить: показывает и активную,
        и завершённую задачу, включая текущую (ещё не завершённую) команду.
        """
        if not task:
            return "🆕 У вас нет задач. Напишите задачу — и я начну.", None
        emoji = self._task_status_emoji(task.status)
        if self._killed_by_openhands(task):
            emoji = "🪓"
        layers = await self._layer_status_block(task)
        activity = await self._collect_task_logs(task, lines=6)
        pending = ""
        if self._has_pending_question(task.id):
            q = self._strip_tool_markup(
                str(self._pending_agent_questions[task.id].get("question") or "")
            ).strip()
            pending = (
                f"\n\n❓ Ждёт ответа: {self._short_cmd(q, max_len=300)}\n"
                "Ответь кнопкой ниже — или комментарием в issue."
            )
        killed = (
            "\n\n🪓 Агент остановлен и убит OpenHands (зацикливание). "
            "Написанное сохранено в контейнере — задачу можно перезапустить."
            if self._killed_by_openhands(task)
            else ""
        )
        text = (
            f"{emoji} Задача: «{task.title[:60]}»\n"
            f"Статус: {self._task_status_label(task.status)}\n"
            f"Модель: {self._effective_model(task)}\n"
            f"Стоимость: {self._cost_line(task)}\n"
            f"PR: {task.pr_url or '—'}\n\n"
            f"📊 Слои:\n{layers}\n\n"
            f"📋 Что делает агент:\n{activity}"
            f"{killed}{pending}"
        )
        return text, self._task_actions_keyboard(task)

    def _task_card(self, task: Task) -> str:
        emoji = self._task_status_emoji(task.status)
        if self._killed_by_openhands(task):
            emoji = "🪓"
        elif self._has_pending_question(task.id):
            emoji = "❓"
        pr = task.pr_url or "—"
        model = self._effective_model(task)
        created = task.created_at.strftime("%Y-%m-%d %H:%M")
        src = "GitHub" if task.source == TaskSource.GITHUB else "Telegram"
        killed = (
            "\n🪓 Убита OpenHands: агент зациклился (написанное сохранено)"
            if self._killed_by_openhands(task)
            else ""
        )
        card = (
            f"{emoji} {task.title[:70]}\n"
            f"ID: {task.id}\n"
            f"Источник: {self._source_tag(task)} {src}\n"
            f"Статус: {self._task_status_label(task.status)}\n"
            f"Модель: {model}\n"
            f"Попытка: {task.attempt}\n"
            f"Стоимость: {self._cost_line(task)}\n"
            f"PR: {pr}\n"
            f"Создана: {created}{killed}"
        )
        return card

    def _cost_line(self, task: Task) -> str:
        if not task.tokens_prompt and not task.tokens_completion and not task.cost_rub:
            return "—"
        return (
            f"{task.cost_rub:.2f} ₽ "
            f"(вход {task.tokens_prompt:,} / выход {task.tokens_completion:,} ток.)"
            .replace(",", " ")
        )

    async def _send_task_list(self, chat_id_int: int, chat_id: str, page: int,
                              message_id: Optional[int] = None) -> None:
        tasks = self.state.list_recent(limit=50)
        per_page = 5
        pages = max(1, (len(tasks) + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        chunk = tasks[page * per_page:(page + 1) * per_page]

        if not tasks:
            text = "📋 Пока нет задач. Напишите задачу — и я начну."
            await self.telegram.send_message(chat_id_int, text)
            return

        lines = [f"📋 Задачи ({len(tasks)}), стр. {page + 1}/{pages}:\n"]
        for t in chunk:
            emoji = self._task_status_emoji(t.status)
            if self._killed_by_openhands(t):
                emoji = "🪓"
            elif self._has_pending_question(t.id):
                emoji = "❓"  # висит вопрос агента — требует внимания
            pr = t.pr_url or "—"
            lines.append(f"{emoji} {self._source_tag(t)} {t.title[:55]}\n     PR: {pr}\n")
        text = "\n".join(lines)

        rows: list[list[tuple[str, str]]] = []
        for i in range(0, len(chunk), 2):
            row = []
            for t in chunk[i:i + 2]:
                emoji = self._task_status_emoji(t.status)
                row.append((f"{emoji} {t.title[:24]}", f"t:{t.id}"))
            rows.append(row)

        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("⬅️", f"tasks:{page - 1}"))
        nav.append((f"📄 {page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(("➡️", f"tasks:{page + 1}"))
        rows.append(nav)

        keyboard = self.telegram.build_keyboard(rows)
        if message_id:
            await self.telegram.edit_message(chat_id_int, message_id, text, keyboard)
        else:
            await self.telegram.send_message(chat_id_int, text, keyboard)

    async def _send_task_detail(self, query: dict, task: Task) -> None:
        activity = await self._collect_task_logs(task, lines=6)
        text = f"{self._task_card(task)}\n\n📋 Последняя активность:\n{activity}"
        await self._reply(query, text, self._task_actions_keyboard(task))

    # Признаки того, что агент закончил работу, а упало уже после — на
    # синхронизации/ветке/пуше/PR (финализация). Такие задачи можно
    # «подтолкнуть»: повторить финализацию без перезапуска агента.
    _FINALIZE_FAILURE_MARKERS = (
        "Из контейнера:",                          # правки вытащены — упали дальше
        "git/push failed",                         # упали на ветке/пуше
        "Синхронизация из контейнера не удалась",  # упали на синхронизации
        "Финализация упала",                       # упали на PR/ревьюерах/артефактах
    )

    @classmethod
    def _can_nudge_finalization(cls, task: Task) -> bool:
        """Задача упала после работы агента — на коммите/пуше/PR.

        Условия: задача failed (или done, но PR так и не создался — например,
        create_pr упал уже после пуша), PR нет, и в логе есть след упавшей
        финализации. Полный перезапуск такой задачи неоправданно дорог
        (агент уже сделал всю работу) — вместо этого «подталкиваем»:
        ветка → пуш → PR из готовой рабочей копии.
        """
        if task.pr_url:
            return False
        if task.status not in (TaskStatus.FAILED, TaskStatus.DONE):
            return False
        log_text = "\n".join(task.log)
        return any(marker in log_text for marker in cls._FINALIZE_FAILURE_MARKERS)

    def _start_nudge(self, task: Task) -> None:
        """Включить режим подталкивания: финализация без перезапуска агента.

        Порядок в очереди общий (задачи выполняются строго по одной);
        workspace, ветку и попытку НЕ трогаем — пуш уйдёт в ту же ветку.
        """
        task.status = TaskStatus.RUNNING
        task.finalize_only = True
        task.add_log("Подталкивание: повторная финализация (ветка → пуш → PR)")
        self.state.save(task)
        logger.info("[nudge] task %s queued for finalization", task.id)

    async def _handle_nudge_command(self, chat_id: str) -> None:
        """Кнопка «Подтолкнуть» / команда /nudge.

        Берём свежую упавшую после работы агента задачу этого чата; если своей
        нет (например, упала GitHub-задача) — последнюю подходящую из недавних.
        """
        chat_id_int = int(chat_id)
        candidates = [
            t for t in self.state.list_for_chat(chat_id, limit=20)
            if self._can_nudge_finalization(t)
        ]
        fallback_note = ""
        if not candidates:
            candidates = [
                t for t in self.state.list_recent(limit=20)
                if self._can_nudge_finalization(t)
            ]
            fallback_note = "\n(задач этого чата нет — взята последняя упавшая)"
        if not candidates:
            await self.telegram.send_message(
                chat_id_int,
                "🆕 Задач для подталкивания нет.\n\n"
                "Подталкивание продолжает задачу, упавшую ПОСЛЕ работы агента "
                "(на коммите/пуше/PR). Для остальных есть «🔁 Перезапустить» "
                "в карточке задачи.",
            )
            return
        task = candidates[0]
        active = self._get_any_active_task()
        if active:
            await self.telegram.send_message(
                chat_id_int,
                f"⚠️ Сейчас выполняется «{(active.title or '')[:40]}…» — "
                "подтолкнуть можно, когда она завершится.",
            )
            return
        self._start_nudge(task)
        position = self._task_queue.qsize() + 1
        queue_note = f"Позиция в очереди: {position}. " if position > 1 else ""
        await self.telegram.send_message(
            chat_id_int,
            f"📤 Подталкиваю {self._source_tag(task)} «{task.title[:50]}…» — "
            f"продолжаю с этапа коммита/пуша, агента заново НЕ запускаю.\n"
            f"{queue_note}Сообщу о результате.{fallback_note}",
        )
        self._enqueue_task(task)

    async def _restart_task(self, task: Task, query: dict) -> None:
        cqid = query.get("callback_query_id", "")
        if task.status == TaskStatus.RUNNING:
            await self.telegram.answer_callback_query(cqid, "⚠️ Задача уже выполняется")
            return
        active = self._get_any_active_task(exclude_id=task.id)
        if active:
            await self.telegram.answer_callback_query(
                cqid, f"⚠️ Занята задача «{(active.title or '')[:24]}» — дождитесь"
            )
            return

        task.attempt += 1
        task.status = TaskStatus.READY
        task.pr_url = None
        task.conversation_id = None
        task.finalize_only = False  # перезапуск — это снова полный прогон агента
        task.clarification_question = None
        task.clarification_answer = None
        model = self._effective_model(task)
        task.add_log(f"Перезапуск #{task.attempt}, модель: {model}")
        self.state.save(task)
        # Свежий клон — иначе ветка прошлого прогона мешает коммиту.
        shutil.rmtree(self._workspace_for(task), ignore_errors=True)
        logger.info("[restart] task %s attempt %d model %s", task.id, task.attempt, model)
        await self.telegram.answer_callback_query(
            cqid, f"🔄 Перезапуск #{task.attempt} ({model})"
        )
        self._enqueue_task(task)

    async def _on_telegram_callback(self, query: dict) -> None:
        data = (query.get("data") or "").strip()
        chat_id = query.get("chat_id")
        chat_id_str = str(chat_id) if chat_id is not None else ""
        chat_id_int = int(chat_id) if chat_id is not None else 0

        if data == "noop":
            return

        if data.startswith("ans:"):
            _, ans_task_id, ans_idx = data.split(":", 2)
            qinfo = self._pending_agent_questions.get(ans_task_id)
            if qinfo:
                opts = qinfo["options"]
                try:
                    i = int(ans_idx)
                except (TypeError, ValueError):
                    i = -1
                answer = opts[i] if 0 <= i < len(opts) else ""
                if answer:
                    fut = qinfo["future"]
                    if fut and not fut.done():
                        fut.set_result(answer)
                        await self.telegram.answer_callback_query(
                            query["callback_query_id"], f"✅ {answer[:32]}"
                        )
                    else:
                        await self.telegram.answer_callback_query(
                            query["callback_query_id"], "⌛ Вопрос уже закрыт"
                        )
            return

        if data.startswith("qask:"):
            # Ответ админа на вопрос агента следующим сообщением.
            task_id = data[len("qask:"):]
            if not self._is_admin(query.get("username")):
                await self.telegram.answer_callback_query(
                    query.get("callback_query_id", ""), "🔒 Только для админа"
                )
                return
            if not self._has_pending_question(task_id):
                await self.telegram.answer_callback_query(
                    query.get("callback_query_id", ""), "⌛ Вопрос уже закрыт"
                )
                return
            self._admin_answer_chat[chat_id_str] = task_id
            qtask = self._pending_agent_questions[task_id]["task"]
            await self.telegram.send_message(
                chat_id_int,
                "✍️ Напиши ответ следующим сообщением (× — отменить) — "
                f"отправлю агенту задачи «{qtask.title[:40]}…».",
            )
            return

        if data == "menu":
            text = (
                "🤖 Личный помощник по Flutter-задачам.\n\n"
                f"Модель по умолчанию: {self._default_model()}\n\n"
                "Напишите задачу текстом — или выберите действие:"
            )
            kb = self.telegram.build_keyboard([
                [("📋 Мои задачи", "tasks:0"), ("🧠 Модель по умолчанию", "model:default")],
            ])
            await self._reply(query, text, kb)
            return

        if data.startswith("tasks:"):
            page = int(data.split(":", 1)[1] or 0)
            await self._send_task_list(chat_id_int, chat_id_str, page,
                                       message_id=query.get("message_id"))
            return

        if data.startswith("refresh:"):
            task = self.state.get(data.split(":", 1)[1])
            text, keyboard = await self._render_status_view(task)
            await self._reply(query, text, keyboard)
            return

        if data.startswith("cancel:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                cqid = query.get("callback_query_id", "")
                if task.status not in (
                    TaskStatus.NEW,
                    TaskStatus.CLARIFYING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                ):
                    await self.telegram.answer_callback_query(
                        cqid, "⚠️ Задача уже завершена"
                    )
                else:
                    self._cancel_task(
                        task, "Отменено пользователем (кнопка в карточке)"
                    )
                    await self.telegram.answer_callback_query(
                        cqid, "🚫 Задача отменена"
                    )
                await self._send_task_detail(query, task)
            return

        if data.startswith("t:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                await self._send_task_detail(query, task)
            return

        if data.startswith("logs:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                activity = await self._collect_task_logs(task, lines=20)
                text = f"📖 Логи задачи «{task.title[:50]}»:\n\n{activity}"
                kb = self.telegram.build_keyboard([[("🔙 Назад", f"t:{task.id}")]])
                await self._reply(query, text, kb)
            return

        if data.startswith("restart:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                await self._restart_task(task, query)
                await self._send_task_detail(query, task)
            return

        if data.startswith("nudge:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                cqid = query.get("callback_query_id", "")
                if task.status == TaskStatus.RUNNING:
                    await self.telegram.answer_callback_query(
                        cqid, "⚠️ Задача уже выполняется")
                elif self._get_any_active_task(exclude_id=task.id):
                    await self.telegram.answer_callback_query(
                        cqid, "⚠️ Занята другая задача — дождитесь")
                else:
                    self._start_nudge(task)
                    await self.telegram.answer_callback_query(
                        cqid, "📤 Подталкиваю: ветка → пуш → PR")
                    self._enqueue_task(task)
                    await self._send_task_detail(query, task)
            return

        if data.startswith("delask:"):
            task = self.state.get(data.split(":", 1)[1])
            if task:
                kb = self.telegram.build_keyboard([
                    [("🗑 Да, удалить", f"del:{task.id}"),
                     ("❌ Отмена", f"t:{task.id}")],
                ])
                await self._reply(
                    query,
                    f"🗑 Удалить задачу «{task.title[:60]}»?\n\n"
                    "Это необратимо — задача исчезнет из истории.",
                    kb,
                )
            return

        if data.startswith("del:"):
            task_id = data.split(":", 1)[1]
            task = self.state.get(task_id)
            if not task:
                await self._reply(query, "Задача не найдена.")
                return
            if task.status == TaskStatus.RUNNING:
                await self._reply(query, "⚠️ Задача сейчас выполняется — сначала остановите её.")
                return
            title = task.title[:60]
            self.state.delete(task_id)
            await self._reply(query, f"🗑 Задача «{title}» удалена из истории.")
            await self._send_task_list(chat_id_int, chat_id_str, 0,
                                       message_id=query.get("message_id"))
            return

        if data.startswith("model:"):
            target = data.split(":", 1)[1]
            current = self._default_model() if target == "default" else (
                self._effective_model(self.state.get(target)) if self.state.get(target) else ""
            )
            # Догружаем список моделей, если ещё не кэширован.
            await self._fetch_models()
            text, rows = self._model_picker(target, 0, "s", current)
            await self._reply(query, text, self.telegram.build_keyboard(rows))
            return

        if data.startswith("mp:"):
            _, target, page, mode = data.split(":")
            current = self._default_model() if target == "default" else (
                self._effective_model(self.state.get(target)) if self.state.get(target) else ""
            )
            await self._fetch_models()
            text, rows = self._model_picker(target, int(page), mode, current)
            await self._reply(query, text, self.telegram.build_keyboard(rows))
            return

        if data.startswith("ms:"):
            _, target, page, mode, model = data.split(":", 4)
            if target == "default":
                self._set_default_model(model)
                await self.telegram.answer_callback_query(
                    query["callback_query_id"], f"Модель по умолчанию: {model}"
                )
                text, rows = self._model_picker(target, int(page), mode, model)
                await self._reply(query, text, self.telegram.build_keyboard(rows))
            else:
                task = self.state.get(target)
                if task:
                    task.model = model
                    task.add_log(f"Выбрана модель: {model}")
                    self.state.save(task)
                    await self.telegram.answer_callback_query(
                        query["callback_query_id"], f"Модель задачи: {model}"
                    )
                    await self._send_task_detail(query, task)
            return

        if data.startswith("back:"):
            target = data.split(":", 1)[1]
            if target == "default":
                text = f"🤖 Меню. Модель по умолчанию: {self._default_model()}"
                kb = self.telegram.build_keyboard([
                    [("📋 Мои задачи", "tasks:0"), ("🧠 Модель по умолчанию", "model:default")],
                ])
                await self._reply(query, text, kb)
            else:
                task = self.state.get(target)
                if task:
                    await self._send_task_detail(query, task)
            return

    # ------------------------------------------------------------------ #
    # Основная обработка задачи (вызывается в фоне через create_task)
    # ------------------------------------------------------------------ #
    async def _process_task(self, task: Task) -> None:
        # Объект задачи мог устареть, пока она ждала в очереди, — берём из БД.
        fresh = self.state.get(task.id)
        if fresh is not None:
            task = fresh
        logger.info("[task] %s status=%s", task.id, task.status.value)

        if task.finalize_only:
            # Подталкивание: агент уже отработал, правки в рабочей копии —
            # сразу финализация (ветка → пуш → PR), без запуска агента.
            task.finalize_only = False  # one-shot: сбрасываем до финализации
            self.state.save(task)
            ws = self._ensure_workspace(task)
            client_dir = os.path.join(ws, "social_mind")
            task.add_log("Финализация без агента: ветка → пуш → PR")
            self.state.save(task)
            try:
                await self._build_ios_and_create_pr(task, client_dir, nudged=True)
            except Exception as exc:  # noqa: BLE001 — не оставлять задачу в RUNNING
                logger.error("[nudge] task %s finalize crashed: %s", task.id, exc)
                task.status = TaskStatus.FAILED
                task.add_log(f"Финализация упала: {exc}")
                self.state.save(task)
                self._notify_author(
                    task,
                    f"❌ Не удалось подтолкнуть задачу (этап коммита/пуша/PR).\n\n"
                    f"Ошибка: {exc}",
                )
            return

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

        if task.status == TaskStatus.RUNNING:
            # Восстановление после сбоя (через очередь): разговор мог уцелеть.
            if task.conversation_id:
                info = await self.openhands.get_agent_state(task.conversation_id)
                if info.get("agent_state") is not None:
                    ws = self._ensure_workspace(task)
                    client_dir = os.path.join(ws, "social_mind")
                    task.add_log("Восстановление: переподключение к разговору OpenHands")
                    self.state.save(task)
                    await self._monitor_openhands(task, task.conversation_id, client_dir)
                    return
                task.add_log("Восстановление: разговор OpenHands недоступен — перезапуск")
                self.state.save(task)
            # Разговор мёртв либо не успел создаться: ограниченный перезапуск.
            if task.attempt >= self.max_resume_attempts:
                task.status = TaskStatus.FAILED
                task.add_log(f"Превышен лимит перезапусков ({task.attempt})")
                await self._finalize_cost(task)
                self.state.save(task)
                self._notify_author(
                    task,
                    f"❌ Задача перезапускалась после сбоев {task.attempt} раз и остановлена.",
                )
                self._notify_admin(
                    f"❌ Задача {task.id} остановлена: превышен лимит перезапусков."
                )
                return
            task.attempt += 1
            task.add_log(f"Перезапуск после сбоя: попытка {task.attempt}")
            task.status = TaskStatus.READY
            self.state.save(task)

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
                comment_id = await asyncio.to_thread(
                    self.github_for(task.repo).create_comment, num, f"❓ {question}"
                )
                self._pending_clarifications[task.id] = {
                    "task": task,
                    "future": None,
                    "asked_at": datetime.utcnow(),
                    "github_issue": num,
                    # Ответом считаем только комментарии ПОСЛЕ нашего вопроса.
                    "github_after_comment_id": comment_id,
                }
            except Exception as exc:
                logger.error("[clarify] github comment failed: %s", exc)
                task.status = TaskStatus.FAILED
                self.state.save(task)

    async def _confirm_clarification_accepted(self, task: Task) -> None:
        """Подтвердить автору в исходном канале, что уточнение принято."""
        text = f"✅ Уточнение принято. Начинаю обработку задачи: «{task.title[:50]}…»"
        if task.source == TaskSource.TELEGRAM:
            try:
                await self.telegram.send_message(int(task.source_id), text)
            except Exception as exc:
                logger.error("[telegram] failed to send clarification confirmation: %s", exc)
        elif task.source == TaskSource.GITHUB:
            try:
                await asyncio.to_thread(
                    self.github_for(task.repo).create_comment, int(task.source_id), text
                )
            except Exception as exc:
                logger.error("[clarify] github confirmation failed: %s", exc)

    async def _wait_for_clarification(self, task: Task) -> None:
        info = self._pending_clarifications.get(task.id)
        asked_at: datetime = (info or {}).get("asked_at") or task.created_at
        answer = task.clarification_answer

        timeout = timedelta(seconds=self.config.clarification_timeout_seconds)
        if answer is None and datetime.utcnow() - asked_at > timeout:
            logger.warning("[clarify] timeout for task %s", task.id)
            timeout_h = self.config.clarification_timeout_seconds // 3600
            timeout_txt = (
                f"{timeout_h // 24} сут."
                if timeout_h >= 24 and timeout_h % 24 == 0
                else f"{timeout_h} ч."
            )
            self._notify_author(
                task,
                f"⏰ Время на уточнение истекло ({timeout_txt}). Задача помечена как застывшая.",
            )
            task.status = TaskStatus.FAILED
            task.clarification_answer = None
            self.state.save(task)
            self._pending_clarifications.pop(task.id, None)
            return

        if answer is None and info is None:
            # Рестарт/сбой потерял состояние вопроса (живёт в памяти).
            # Переспрашиваем — иначе задача молча падала бы после каждого
            # перезапуска сервиса во время ожидания уточнений.
            logger.warning("[clarify] %s: pending state lost, re-asking", task.id)
            await self._ask_clarification(task)
            return

        if answer is None and info:
            fut = info.get("future")
            if fut and not fut.done():
                logger.info(
                    "[clarify] task %s waiting for future (poll=%ds)",
                    task.id, self.config.clarification_poll_seconds,
                )
                try:
                    answer = await asyncio.wait_for(
                        fut, timeout=self.config.clarification_poll_seconds
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "[clarify] task %s future timeout, will retry on next tick",
                        task.id,
                    )
                    return

        if not answer:
            return

        # Ответ получен. Запуск — ТОЛЬКО в фоне: _run_openhands живёт часами,
        # и если ждать его прямо здесь, встанет весь главный цикл (poll_github,
        # ответы на вопросы агента, статусы). Для GitHub-задач это вообще
        # дедлок: ответ на вопрос агента собирает тот же главный цикл.
        task.description += f"\n\nУточнение пользователя: {answer}"
        task.clarification_answer = answer
        task.status = TaskStatus.READY
        self.state.save(task)
        self._pending_clarifications.pop(task.id, None)
        await self._confirm_clarification_accepted(task)
        logger.info("[clarify] task %s answer accepted, starting openhands in background", task.id)
        self._spawn(self._run_openhands(task))

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
    def _inject_build_secrets(self, client_dir: str) -> bool:
        """Положить build-секреты (Firebase) в хост-копию workspace.

        Это копия для шага iOS-сборки на iMac; агенту внутри runtime-контейнера
        она не видна — для агента есть _ensure_build_secrets_in_runtime.
        Секреты приходят в config.secrets_dir (кладёт self-hosted runner job
        deploy/sync-build-secrets.yml). Если их нет — просто False: сборка
        продолжается без Firebase-конфига (для валидации обычно достаточно).
        """
        src = self.config.secrets_dir
        available = False

        gs_src = os.path.join(src, "google-services.json")
        android_app = os.path.join(client_dir, "domains", "client", "android", "app")
        if os.path.exists(gs_src) and os.path.isdir(android_app):
            try:
                shutil.copyfile(gs_src, os.path.join(android_app, "google-services.json"))
                available = True
                logger.info("[secrets] google-services.json установлен в workspace")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[secrets] copy google-services.json failed: %s", exc)

        env_src = os.path.join(src, "build.env")
        client_app = os.path.join(client_dir, "domains", "client")
        if os.path.exists(env_src) and os.path.isdir(client_app):
            try:
                shutil.copyfile(env_src, os.path.join(client_app, "build.env"))
                available = True
                logger.info("[secrets] build.env установлен в workspace")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[secrets] copy build.env failed: %s", exc)

        # Вторая линия защиты (как в runtime-контейнере): .git/info/exclude,
        # чтобы git не видел секреты даже без pathspec-исключений.
        git_dir = os.path.join(client_dir, ".git")
        if available and os.path.isdir(git_dir):
            try:
                exclude_path = os.path.join(git_dir, "info", "exclude")
                os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
                with open(exclude_path, "a", encoding="utf-8") as fh:
                    for name in self._SECRET_FILE_NAMES:
                        fh.write(f"\n**/{name}")
                logger.info("[secrets] .git/info/exclude дополнен")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[secrets] git exclude write failed: %s", exc)

        return available

    _SECRET_FILE_NAMES = ("google-services.json", "build.env")

    def _has_build_secrets(self) -> bool:
        """Есть ли build-секреты в config.secrets_dir."""
        src = self.config.secrets_dir
        return any(
            os.path.exists(os.path.join(src, name)) for name in self._SECRET_FILE_NAMES
        )

    def _imac_ssh_configured(self) -> bool:
        """iMac настроен (host/user) и SSH-ключ существует на хосте.

        Нужен агенту: iOS-сборку он делает сам, по SSH с iMac (macOS-only
        шаг, в Linux-контейнере невозможен).
        """
        key = os.path.expanduser(self.config.imac_ssh_key)
        return bool(
            self.config.imac_host
            and self.config.imac_user
            and os.path.exists(key)
        )

    async def _ensure_imac_ssh_in_runtime(
        self, conversation_id: str, wait_for_container: bool = False
    ) -> bool:
        """Прокинуть SSH-ключ iMac внутрь runtime-контейнера.

        Агент сам ходит на iMac по SSH и интерпретирует результаты сборки —
        от них зависят его дальнейшие действия. Ключ кладём ТОЛЬКО в
        /tmp/pw-imac-key: в /workspace/social_mind нельзя — он оказался бы
        в git-репозитории и уехал бы в PR.

        True — ключ установлен (или iMac не настроен). False — контейнер ещё
        не готов, повторить позже.
        """
        if not self._imac_ssh_configured():
            return True
        key = os.path.expanduser(self.config.imac_ssh_key)
        container = f"openhands-runtime-{conversation_id}"

        # Контейнер должен быть запущен (runtime стартует не мгновенно).
        running = False
        attempts = 23 if wait_for_container else 1
        for _ in range(attempts):
            code, out, _ = await self._docker(
                "inspect", "--format", "{{.State.Running}}", container, timeout=20
            )
            if code == 0 and out.strip() == "true":
                running = True
                break
            await asyncio.sleep(2)
        if not running:
            logger.warning("[imac-ssh] runtime-контейнер %s не запущен", container)
            return False

        code, _, err = await self._docker(
            "cp", key, f"{container}:/tmp/pw-imac-key", timeout=60
        )
        if code != 0:
            logger.warning("[imac-ssh] docker cp key failed: %s", err.strip())
            return False
        code, _, _ = await self._docker(
            "exec", container, "chmod", "600", "/tmp/pw-imac-key", timeout=20
        )
        if code != 0:
            logger.warning("[imac-ssh] chmod 600 on key failed")
            return False
        return True

    async def _docker(self, *args: str, timeout: float = 60) -> tuple[int, str, str]:
        """Выполнить docker-команду на хосте (вернёт rc, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"docker {' '.join(args)}: timeout {timeout}s"
        return (
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    async def _ensure_build_secrets_in_runtime(
        self, conversation_id: str, wait_for_container: bool = False
    ) -> bool:
        """Прокинуть build-секреты внутрь runtime-контейнера.

        Контейнер ничего не монтирует (Mounts: []), а агент работает в
        /workspace/social_mind ВНУТРИ контейнера — хост-копия ему не видна.
        Поэтому кладём файлы через docker cp в два места:
        - /tmp/pw-secrets/ — как только контейнер запущен (каталог создаётся
          всегда; запасной путь для агента);
        - /workspace/social_mind/... — когда OpenHands уже склонировал
          репозиторий (основное место для сборки).

        True — установлено (или нечего ставить). False — контейнер или
        workspace ещё не готовы, повторить позже.
        """
        if not self._has_build_secrets():
            return True

        container = f"openhands-runtime-{conversation_id}"
        src = self.config.secrets_dir

        # 1. Контейнер должен быть запущен (runtime стартует не мгновенно).
        running = False
        attempts = 23 if wait_for_container else 1
        for _ in range(attempts):
            code, out, _ = await self._docker(
                "inspect", "--format", "{{.State.Running}}", container, timeout=20
            )
            if code == 0 and out.strip() == "true":
                running = True
                break
            await asyncio.sleep(2)
        if not running:
            logger.warning("[secrets] runtime-контейнер %s не запущен", container)
            return False

        # 2. Стейджинг: /tmp/pw-secrets существует всегда.
        await self._docker("exec", container, "mkdir", "-p", "/tmp/pw-secrets")
        for name in self._SECRET_FILE_NAMES:
            fsrc = os.path.join(src, name)
            if not os.path.exists(fsrc):
                continue
            code, _, err = await self._docker(
                "cp", fsrc, f"{container}:/tmp/pw-secrets/{name}", timeout=60
            )
            if code != 0:
                logger.warning("[secrets] docker cp %s failed: %s", name, err.strip())

        # 3. Основное место: /workspace/..., когда репозиторий уже склонирован.
        code, out, _ = await self._run_in_container(
            container,
            "c=0; [ -d /workspace/social_mind/domains/client ] && c=1; "
            "a=0; [ -d /workspace/social_mind/domains/client/android/app ] && a=1; "
            'echo "$c $a"',
        )
        if code != 0:
            return False
        try:
            client_ok, android_ok = (int(x) for x in out.split()[:2])
        except ValueError:
            return False
        if not (client_ok or android_ok):
            return False  # репозиторий ещё не склонирован — повторим позже

        dst = "/workspace/social_mind/domains/client"
        targets: list[tuple[str, str]] = []
        if client_ok and os.path.exists(os.path.join(src, "build.env")):
            targets.append(("build.env", f"{dst}/build.env"))
        if android_ok and os.path.exists(os.path.join(src, "google-services.json")):
            targets.append(
                ("google-services.json", f"{dst}/android/app/google-services.json")
            )
        for name, target in targets:
            code, _, err = await self._docker(
                "cp", os.path.join(src, name), f"{container}:{target}", timeout=60
            )
            if code != 0:
                logger.warning("[secrets] install %s failed: %s", name, err.strip())
                return False

        # 4. Права + .git/info/exclude: чтобы агент не закоммитил секреты
        #    внутри контейнера и они не уехали в PR при синхронизации.
        fix = (
            "set -e; "
            "chmod 600 /tmp/pw-secrets/* 2>/dev/null || true; "
            f"for f in {dst}/build.env {dst}/android/app/google-services.json; do "
            '  [ -f "$f" ] && chmod 600 "$f" || true; '
            "done; "
            "ex=/workspace/social_mind/.git/info/exclude; "
            "if [ -d /workspace/social_mind/.git ]; then "
            '  mkdir -p "$(dirname "$ex")"; touch "$ex"; '
            "  for f in build.env google-services.json "
            "domains/client/build.env domains/client/android/app/google-services.json; do "
            '    grep -qxF "$f" "$ex" || echo "$f" >> "$ex"; '
            "  done; "
            "fi"
        )
        code, _, err = await self._run_in_container(container, fix)
        if code != 0:
            logger.warning("[secrets] chmod/exclude failed: %s", err.strip())

        logger.info("[secrets] build-секреты установлены в %s", container)
        return True

    async def _run_openhands(self, task: Task) -> None:
        """Защита от двойного запуска одной задачи.

        Задачу могут «продолжить» одновременно два пути (главный цикл после
        уточнения и фоновый _process_task) — без защиты создались бы два
        разговора OpenHands и две ветки PR.
        """
        if task.id in self._running_tasks:
            logger.warning(
                "[task] %s уже выполняется — дублирующий запуск пропущен", task.id
            )
            return
        self._running_tasks.add(task.id)
        try:
            await self._run_openhands_impl(task)
        finally:
            self._running_tasks.discard(task.id)
            # Runtime-контейнеры без чистки копятся и съедают память хоста:
            # один контейнер с Gradle/Kotlin daemon — до 6 ГБ. После 18
            # накопленных контейнеров активной задаче не хватало памяти,
            # bash-сервер рантайма умирал (exit code -1 на любых командах)
            # и OpenHands убивал задачу «за зацикливание».
            # Задача завершена (любым исходом) — контейнер больше не нужен:
            # правки агента уже синхронизированы на хост, логи — в БД.
            await self._cleanup_runtime_container(task)

    async def _cleanup_runtime_container(self, task: Task) -> None:
        """Удалить runtime-контейнер завершённой задачи (best-effort).

        Исключение — задача с PR: контейнер понадобится CI-вотчеру
        (замкнутый цикл «упавший CI → правки агента → допуш») и циклу
        ревью; их чистит отдельный путь (_watch_gha_artifacts_loop /
        _handle_pr_feedback после использования). Здесь чистим задачи
        без PR — упавшие, убитые, отменённые, «нет изменений».
        """
        if not task.conversation_id:
            return
        if task.pr_url:
            return
        container = f"openhands-runtime-{task.conversation_id}"
        try:
            code, _, _ = await self._docker(
                "rm", "-f", container, timeout=60
            )
            if code == 0:
                logger.info("[cleanup] runtime container %s removed", container)
                task.add_log("Runtime-контейнер удалён (память хоста)")
                self.state.save(task)
        except Exception as exc:  # noqa: BLE001 — чистка не должна падать
            logger.warning("[cleanup] %s remove failed: %s", container, exc)

    async def _gc_runtime_containers(self) -> None:
        """Чистка осиротевших ЗАПУЩЕННЫХ runtime-контейнеров (раз в час).

        Память хоста едят только контейнеры в состоянии running (gradle-/
        kotlin-daemon — до 6 ГБ); остановленные почти бесплатны — только
        диск. Поэтому GC удаляет исключительно осиротевшие running:
        остановленные контейнеры PR-задач остаются жить на случай цикла
        ревью (комментарии могут прийти через дни после снятия маркера
        вотчера), не-PR убираются сразу в _cleanup_runtime_container.
        """
        active_cids = {
            t.conversation_id
            for status in (
                TaskStatus.NEW,
                TaskStatus.CLARIFYING,
                TaskStatus.READY,
                TaskStatus.RUNNING,
            )
            for t in self.state.list_by_status(status)
            if t.conversation_id
        }
        code, out, _ = await self._docker(
            "ps", "--filter", "status=running",
            "--format", "{{.ID}} {{.Names}}", timeout=30,
        )
        if code != 0:
            return
        # PR-задачи под живым вотчером защищены всегда (маркер .watch).
        watchers_dir = Path(self.config.state_db).parent / "gha_watchers"
        try:
            watcher_alive = any(watchers_dir.glob("*.watch"))
        except OSError:
            watcher_alive = False
        removed = 0
        for line in out.splitlines():
            cid, _, name = line.partition(" ")
            if not name.startswith("openhands-runtime-"):
                continue
            conversation_id = name.removeprefix("openhands-runtime-")
            if conversation_id in active_cids:
                continue
            if watcher_alive and self._task_pr_by_cid(conversation_id):
                continue  # PR-задача, вотчер жив — контейнер нужен циклу допуша
            rc, _, err = await self._docker("rm", "-f", cid, timeout=60)
            if rc == 0:
                removed += 1
            else:
                logger.warning("[gc] rm %s failed: %s", name, err.strip()[:100])
        if removed:
            logger.info(
                "[gc] removed %d orphan running container(s)", removed
            )

    def _task_pr_by_cid(self, conversation_id: str) -> bool:
        """Есть ли задача с PR и этим разговором (кандидат под вотчером)."""
        return any(
            t.conversation_id == conversation_id and t.pr_url
            for status in (TaskStatus.DONE,)
            for t in self.state.list_by_status(status)
        )

    async def _run_openhands_impl(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        self.state.save(task)

        ws = self._ensure_workspace(task)
        client_dir = os.path.join(ws, "social_mind")
        repo_url = f"https://github.com/{self._repo_full(task)}.git"

        # Клонируем репозиторий в рабочую директорию
        if not os.path.exists(os.path.join(client_dir, ".git")):
            clone_cmd = ["git", *self._git_auth_args(task.repo), "clone", "--depth", "1", repo_url, client_dir]
            proc = await asyncio.create_subprocess_exec(
                *clone_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.error("[git] clone failed: %s", err.decode("utf-8", errors="replace").strip())

        # Прокидываем build-секреты (Firebase) в workspace, если они есть.
        has_secrets = self._inject_build_secrets(client_dir)

        secrets_note = ""
        if has_secrets:
            secrets_note = (
                "ПРИМЕЧАНИЕ (Firebase, build-секреты):\n"
                "- google-services.json оркестратор кладёт в android/app/ "
                "автоматически; если файла там нет — скопируй его сам: "
                "sudo install -m 600 /tmp/pw-secrets/google-services.json "
                "android/app/google-services.json\n"
                "- Значения --dart-define (CLOSERS_*) — в /tmp/pw-secrets/build.env "
                "(формат KEY=VALUE); при сборке apk/web добавь их через --dart-define.\n"
                "- Это секретные файлы: НЕ коммить их и НЕ показывай их "
                "содержимое в ответах — только используй для сборки.\n\n"
            )

        # Инструкция для агента: внести изменения + запустить тесты и сборки внутри контейнера.
        # Путь — ВНУТРИ runtime-контейнера: там проект лежит в /workspace/<repo>.
        # (Путь /opt/openhands/workspace/... из хоста в контейнере не существует.)
        #
        # Проверка iOS: агент сам ходит на iMac по SSH (ключ кладёт
        # _ensure_imac_ssh_in_runtime в /tmp/pw-imac-key) и интерпретирует
        # результат — от него зависят его дальнейшие действия. Оркестратор
        # iOS повторно не собирает.
        if self._imac_ssh_configured():
            imac_user = self.config.imac_user
            imac_host = self.config.imac_host
            imac_workdir = self.config.imac_work_dir
            ssh = (
                f"ssh -i /tmp/pw-imac-key -o StrictHostKeyChecking=accept-new "
                f"{imac_user}@{imac_host}"
            )
            imac_item = (
                "3. iOS-сборка выполняется НЕ в контейнере (Linux не умеет iOS), "
                "а на отдельном macOS-хосте по SSH. После успешных проверок из "
                "пункта 2 сделай:\n"
                "   a) скопируй проект на iMac (без build-артефактов):\n"
                "      tar -C /workspace/social_mind/domains/client "
                "--exclude=build --exclude=.dart_tool -cf - . | \\\n"
                f"      {ssh} 'mkdir -p {imac_workdir}/client && "
                f"tar -xf - -C {imac_workdir}/client'\n"
                "   b) собери iOS (--no-codesign: валидируем компиляцию/линковку, "
                "а не подпись):\n"
                f"      {ssh} 'cd {imac_workdir}/client && "
                "flutter build ios --release --no-codesign'\n"
                "   c) если сборка упала — исправь код в контейнере и повтори a)–b).\n"
                "   ВАЖНО про тесты на iMac:\n"
                "   - unit-тесты на iMac можно гонять ТОЛЬКО если в репо "
                "есть test/libisar_macos.dylib (Isar.initialize в тестах "
                "требует этот файл; без него isar-тесты падают с "
                "'Failed to lookup symbol isar_version' — это НЕ ошибка "
                "твоего кода, просто файл не добавлен в репозиторий).\n"
                "   - Если dylib есть — прогони на iMac полный сьюит "
                "(flutter test) и исправь падения как обычные.\n"
                "   - Если dylib нет — unit-тесты на iMac не запускай, они "
                "уже прогнаны в контейнере (пункт 2); isar-тесты при этом "
                "падают по причине выше.\n"
                "   - интеграционные тесты в iOS-симуляторе запускай ТОЛЬКО "
                "если задача этого требует. Порядок:\n"
                "     1) запусти Firebase-эмулятор на iMac (если тестам он "
                "нужен): "
                f"{ssh} 'cd {imac_workdir}/client && "
                "nohup firebase emulators:start --only auth,firestore "
                "> /tmp/firebase_emulator.log 2>&1 &'\n"
                "     2) загрузи симулятор и найди UDID:\n"
                f"        {ssh} 'xcrun simctl boot \"iPhone 16\"; "
                "xcrun simctl list devices | grep Booted'\n"
                "        (дождись окончания загрузки: xcrun simctl bootstatus "
                "\"iPhone 16\" -b)\n"
                "     3) прогони тест (первая сборка под симулятор может "
                "занять 10-15 минут из-за pod install — ставь таймаут):\n"
                f"        {ssh} 'cd {imac_workdir}/client && flutter test "
                "integration_test/<файл>_test.dart -d <UDID>'\n"
                "     4) после тестов выключи симулятор и убей эмулятор:\n"
                f"        {ssh} 'xcrun simctl shutdown \"iPhone 16\"; "
                "pkill -f firebase-tools || true'\n"
                "     Из симулятора эмулятор Firebase доступен на localhost "
                "(тот же iMac). Если «iPhone 16» нет в списке — возьми любой "
                "доступный iPhone-симулятор из xcrun simctl list devices.\n"
                "   Если iMac недоступен по SSH (сеть/выключен) — это не ошибка "
                "кода: продолжи работу и явно укажи в финальном отчёте, что iOS "
                "не проверена из-за недоступности iMac.\n"
            )
            tail_items = (
                "4. Если какие-либо команды завершаются с ошибкой — исправь код и запусти снова.\n"
                "5. Сообщи, когда все проверки пройдены успешно.\n"
            )
        else:
            imac_item = ""
            tail_items = (
                "3. Если какие-либо команды завершаются с ошибкой — исправь код и запусти снова.\n"
                "4. Сообщи, когда все проверки пройдены успешно.\n"
            )
        instruction = self._render_instruction(
            description=task.description,
            imac_item=imac_item,
            tail_items=tail_items,
            secrets_note=secrets_note,
        )
        # Настраиваем LLM провайдера (provod.ai) перед каждым разговором —
        # модель берём из задачи (либо модель по умолчанию).
        llm_result = await self.openhands.configure_llm(
            model=self._effective_model(task)
        )
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
            repository=self._repo_full(task),
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

        # Прокидываем build-секреты внутрь runtime-контейнера: он ничего не
        # монтирует, а агент работает в /workspace внутри контейнера. Ждём
        # контейнер; если workspace ещё не склонирован — доложит мониторинг.
        if self._has_build_secrets():
            try:
                staged = await self._ensure_build_secrets_in_runtime(
                    conversation_id, wait_for_container=True
                )
            except Exception as exc:  # noqa: BLE001 — не мешаем старту агента
                logger.warning("[secrets] ensure failed: %s", exc)
                staged = False
            task.add_log(
                "Build secrets: проброшены в контейнер"
                if staged
                else "Build secrets: workspace ещё не готов, установим при мониторинге"
            )
            self.state.save(task)

        # SSH-ключ iMac: агент сам гоняет iOS-сборку на iMac по SSH.
        if self._imac_ssh_configured():
            try:
                imac_staged = await self._ensure_imac_ssh_in_runtime(
                    conversation_id, wait_for_container=True
                )
            except Exception as exc:  # noqa: BLE001 — не мешаем старту агента
                logger.warning("[imac-ssh] ensure failed: %s", exc)
                imac_staged = False
            task.add_log(
                "SSH-ключ iMac проброшен в контейнер"
                if imac_staged
                else "SSH-ключ iMac: контейнер ещё не готов, установим при мониторинге"
            )
            self.state.save(task)

        # Запускаем agente loop
        start_result = await self.openhands.start_conversation(conversation_id)
        if start_result.get("status") == "error":
            error_msg = start_result.get('error', 'неизвестная ошибка')
            logger.warning("[openhands] start_conversation returned error: %s, but will try to monitor anyway", error_msg)
            task.add_log(f"OpenHands start_conversation warning: {error_msg}")
            self.state.save(task)

        await self._monitor_openhands(task, conversation_id, client_dir)

    _QUESTION_OPT_RE = re.compile(r"^\s*(?:[-*•]|\(?\d{1,2}[.)\]]|\(?[A-Za-z][.)])\s+(.+)$")

    def _parse_question_options(self, question: str) -> tuple[str, list[str]]:
        """Извлечь варианты ответа из текста вопроса (нумерованные/буллиты).

        Варианты показываем кнопками; если пунктов меньше 2 — свободный ввод.
        """
        options: list[str] = []
        for ln in question.splitlines():
            m = self._QUESTION_OPT_RE.match(ln)
            if m:
                opt = m.group(1).strip()
                if opt:
                    options.append(opt)
        return question, options

    async def _handle_agent_question(self, task: Task, conversation_id: str,
                                     question: str) -> bool:
        """Показать вопрос агента, дождаться ответа и отправить его в разговор.

        Варианты из вопроса показываются кнопками; свободный текст тоже
        принимается. Ответ уходит в тот же разговор через send_message, чтобы
        агент продолжил работу. True — ответ доставлен.
        """
        text, options = self._parse_question_options(
            self._strip_tool_markup(question)
        )
        fut = asyncio.get_event_loop().create_future()
        # ВАЖНО: держим ссылку qinfo на запись — в GitHub-ветке в неё
        # прописываются github_issue/github_after_comment_id, по которым
        # poll_github находит ответ автора. Без этого задача зависает,
        # пока не истечёт таймаут ожидания ответа.
        qinfo = self._pending_agent_questions[task.id] = {
            "task": task,
            "conversation_id": conversation_id,
            "question": text,
            "options": options,
            "future": fut,
            "asked_at": datetime.utcnow(),
        }

        question_msg_id: Optional[int] = None
        chat_id: Optional[int] = None
        question_comment_id: Optional[int] = None
        if task.source == TaskSource.TELEGRAM:
            chat_id = int(task.source_id)
            keyboard = None
            if len(options) >= 2:
                rows = [[(opt[:48], f"ans:{task.id}:{i}")] for i, opt in enumerate(options)]
                keyboard = self.telegram.build_keyboard(rows)
            question_msg_id = await self.telegram.send_message(chat_id, f"❓ {text}", keyboard=keyboard)
            if keyboard is None:
                await self.telegram.send_message(
                    chat_id, "✍️ Ответьте на вопрос текстом."
                )
        elif task.source == TaskSource.GITHUB:
            try:
                num = int(task.source_id)
                lines = [f"❓ {text}"]
                if len(options) >= 2:
                    lines.append("")
                    lines.extend(f"{i + 1}. {opt}" for i, opt in enumerate(options))
                    lines.append("")
                    lines.append("Ответьте комментарием: номер варианта или свободный текст.")
                else:
                    lines.append("")
                    lines.append("Ответьте комментарием к issue.")
                # create_comment — блокирующий вызов PyGithub: уводим в поток,
                # чтобы не тормозить event loop (и Telegram) на время запроса.
                question_comment_id = await asyncio.to_thread(
                    self.github_for(task.repo).create_comment, num, "\n".join(lines)
                )
                qinfo["github_issue"] = num
                # Ответом считаем только комментарии ПОСЛЕ этого вопроса.
                qinfo["github_after_comment_id"] = question_comment_id
            except Exception as exc:  # noqa: BLE001
                logger.error("[agent-question] github comment failed: %s", exc)
            # Админ: уведомление о вопросе + возможность ответить из Telegram
            # (в issue комментарий уже ушёл автору).
            self._notify_admin_question(task, text, options)

        logger.info("[agent-question] task %s waiting for user answer", task.id)
        try:
            answer = await asyncio.wait_for(
                fut, timeout=self.config.clarification_timeout_seconds
            )
        except asyncio.TimeoutError:
            self._pending_agent_questions.pop(task.id, None)
            if task.source == TaskSource.TELEGRAM and question_msg_id:
                await self._close_question_message(chat_id, question_msg_id, text,
                                                   "⏰ Время ожидания ответа истекло.")
            elif task.source == TaskSource.GITHUB and question_comment_id:
                await self._update_github_question_comment(
                    task, question_comment_id, text, "⏰ Время ожидания ответа истекло."
                )
            return False

        if answer is None:
            # Задача отменена во время ожидания — ответ агенту не отправляем.
            logger.info("[agent-question] task %s cancelled while waiting", task.id)
            return False

        self._pending_agent_questions.pop(task.id, None)
        # Ответ принят — убираем кнопки и фиксируем выбор в сообщении с вопросом.
        if task.source == TaskSource.TELEGRAM and question_msg_id:
            await self._close_question_message(chat_id, question_msg_id, text,
                                               f"✅ Ответ принят: {answer[:200]}")
        elif task.source == TaskSource.GITHUB and question_comment_id:
            await self._update_github_question_comment(
                task, question_comment_id, text, f"✅ Ответ принят: {answer[:200]}"
            )
        try:
            res = await self.openhands.send_message(conversation_id, answer)
            if res.get("status") == "error" or res.get("success") is False:
                # Разговор могли погасить за время долгого ожидания ответа —
                # пробуем разбудить и доставить ещё раз.
                await self.openhands.start_conversation(conversation_id)
                res = await self.openhands.send_message(conversation_id, answer)
                if res.get("status") == "error" or res.get("success") is False:
                    logger.error(
                        "[agent-question] answer delivery failed for %s: %s",
                        conversation_id, res,
                    )
                    return False
            task.add_log(f"Ответ пользователю отправлен агенту: {answer[:60]}")
            self.state.save(task)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent-question] send answer failed: %s", exc)
            return False

    async def _close_question_message(self, chat_id: int, message_id: int,
                                      question: str, footer: str) -> None:
        """Убрать кнопки вопроса и дописать итог (ответ/истечение) в то же сообщение."""
        try:
            await self.telegram.edit_message(
                chat_id, message_id, f"❓ {question}\n\n{footer}", keyboard=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent-question] close question message failed: %s", exc)

    async def _update_github_question_comment(
        self, task: Task, comment_id: int, question: str, footer: str
    ) -> None:
        """Дописать итог (ответ/истечение) в комментарий-вопрос issue."""
        try:
            await asyncio.to_thread(
                self.github_for(task.repo).update_comment,
                comment_id,
                f"❓ {question}\n\n{footer}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent-question] github comment update failed: %s", exc)

    def _has_pending_question(self, task_id: str) -> bool:
        """По задаче сейчас ждётся ответ на вопрос агента."""
        qinfo = self._pending_agent_questions.get(task_id)
        fut = (qinfo or {}).get("future")
        return bool(qinfo and fut and not fut.done())

    def _notify_admin_question(self, task: Task, question: str, options: list[str]) -> None:
        """Админу: вопрос агента по задаче из issue + кнопки ответа.

        Варианты из вопроса — кнопками (обрабатываются общим ans:-колбэком),
        плюс «Свой ответ» (qask) — следующим сообщением в чат. Для Telegram-
        задач не вызывается: вопрос уже ушёл автору в его чат.
        """
        admin_chat = self.config.telegram_admin_chat_id
        if not admin_chat:
            return
        repo = self._repo_full(task)
        body = (
            f"❓ Вопрос агента по задаче «{task.title[:60]}»\n"
            f"{repo} · issue #{task.source_id}\n\n"
            f"{question[:600]}"
        )
        rows = [
            [(opt[:48], f"ans:{task.id}:{i}")] for i, opt in enumerate(options[:4])
        ]
        rows.append([("✍️ Свой ответ", f"qask:{task.id}")])
        try:
            fut = asyncio.ensure_future(
                self.telegram.send_message(
                    int(admin_chat), body, keyboard=self.telegram.build_keyboard(rows)
                )
            )
            self._pending_notifications.add(fut)
            fut.add_done_callback(self._pending_notifications.discard)
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent-question] admin notify failed: %s", exc)

    async def _monitor_openhands(self, task: Task, conversation_id: str, client_dir: str) -> None:
        """Мониторит сессию OpenHands: уточнения, завершение, ошибки.

        Ориентируемся на состояние агента (AgentState), а не на статус
        разговора: разговор переходит в STOPPED и при успехе, и при ошибке,
        поэтому по нему результат не определить.
        """
        state_misses = 0
        # «Холодный старт»: сколько опросов подряд агент висит в loading и в
        # траектории нет ни одного события. Рантайм обычно готов за пару минут;
        # если за loading_timeout агент так и не начал работу — разговор
        # «пустой» (был реальный случай: runtime ready, но начальное сообщение
        # не обработано, разговор висел в loading 35 минут до ручной отмены).
        # Пересоздаём разговор с той же инструкцией.
        loading_polls = 0
        loading_deadline = time.monotonic() + self.loading_startup_timeout_seconds
        # Текст вопроса, на который только что отправили ответ. Нужен, чтобы не
        # задать тот же вопрос повторно: после доставки ответа агент ещё какое-то
        # время остаётся в awaiting_user_input со старым сообщением.
        answered_msg: Optional[str] = None
        # Текст, на который уже отправляли авто-подталкивание: агент после
        # доставки ещё какое-то время висит в awaiting_user_input с тем же
        # сообщением — повторно толкать его нельзя.
        nudged_msg: Optional[str] = None
        # Сколько раз подряд авто-продолжали агента без вопроса.
        auto_continues = 0
        # Сколько раз будили разговор, погасший во время ожидания ответа.
        stop_restarts = 0
        # Сколько раз продолжали работу агента после временной ошибки
        # (недоступность LLM: 503/429/таймаут).
        error_retries = 0
        # Сколько раз продолжали после того, как OpenHands сам остановил
        # агента за зацикливание (AgentStuckInLoopError): один ретрай —
        # свежий старт того же разговора, обычно модель не повторяет цикл.
        stuck_retries = 0
        # Исполненные команды на момент последней паузы — реальный прогресс
        # для сброса счётчика авто-продолжений (work_events OpenHands
        # обнуляет при каждом запуске агента и прогрессу не соответствует).
        last_pause_cmds: Optional[int] = None
        # Подряд идущие паузы с сырым вызовом инструмента текстом
        # (<function=...>) — признак сломанного tool-calling у провайдера.
        raw_tool_pauses = 0
        # Build-секреты: повторяем установку, пока OpenHands не склонировал
        # репозиторий в /workspace (покрывает и старт, и recovery).
        secrets_done = not self._has_build_secrets()
        secrets_tries = 0
        # SSH-ключ iMac: /tmp-стейджинг не зависит от клона репозитория,
        # но ретраим на случай, если контейнер ещё поднимался.
        imac_done = not self._imac_ssh_configured()
        imac_tries = 0
        while True:
            if task.status == TaskStatus.CANCELLED:
                task.add_log("Мониторинг остановлен: задача отменена")
                self.state.save(task)
                return
            if not imac_done:
                imac_tries += 1
                try:
                    imac_done = await self._ensure_imac_ssh_in_runtime(
                        conversation_id
                    )
                except Exception as exc:  # noqa: BLE001 — не роняем мониторинг
                    logger.warning("[imac-ssh] ensure failed: %s", exc)
                    imac_done = False
                if imac_done:
                    task.add_log("SSH-ключ iMac установлен в контейнер")
                elif imac_tries >= 30:
                    imac_done = True  # не ретраим вечно
                    task.add_log(
                        "SSH-ключ iMac: не удалось установить — "
                        "iOS-проверка агентом будет недоступна"
                    )
            if not secrets_done:
                secrets_tries += 1
                try:
                    secrets_done = await self._ensure_build_secrets_in_runtime(
                        conversation_id
                    )
                except Exception as exc:  # noqa: BLE001 — не роняем мониторинг
                    logger.warning("[secrets] ensure failed: %s", exc)
                    secrets_done = False
                if secrets_done:
                    task.add_log("Build secrets установлены в контейнер")
                elif secrets_tries >= 30:
                    secrets_done = True  # не ретраим вечно
                    task.add_log(
                        "Build secrets: не удалось установить в workspace, "
                        "сборка продолжится без Firebase-конфига"
                    )
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
                    await self._finalize_cost(task)
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

            # «Холодный старт»: разговор в loading без единого события дольше
            # допустимого — агентский цикл не обработал начальное сообщение.
            # Стартуем разговор заново; если не помогло — пересоздаём.
            if (
                state_val == "loading"
                and info.get("work_events", 0) == 0
                and time.monotonic() > loading_deadline
            ):
                loading_polls += 1
                if loading_polls <= 2:
                    task.add_log(
                        "Разговор висит в loading без работы — перезапускаю "
                        f"разговор ({loading_polls}/2)"
                    )
                    self.state.save(task)
                    await self.openhands.start_conversation(conversation_id)
                    loading_deadline = (
                        time.monotonic() + self.loading_startup_timeout_seconds
                    )
                    await asyncio.sleep(self.config.clarification_poll_seconds)
                    continue
                task.status = TaskStatus.FAILED
                task.add_log(
                    "OpenHands: разговор не стартовал (вечный loading, "
                    "начальное сообщение не обработано) — ретраи исчерпаны"
                )
                await self._finalize_cost(task)
                self.state.save(task)
                self._notify_author(
                    task,
                    "❌ Разговор OpenHands не смог стартовать: агент так и не "
                    "начал работу после запуска (вечный loading).\n\n"
                    "Перезапуски не помогли. Попробуй перезапустить задачу — "
                    "обычно это разовый сбой OpenHands.",
                )
                if task.source == TaskSource.GITHUB:
                    self._notify_admin(
                        f"❌ Задача {self._repo_full(task)}#{task.source_id} "
                        f"«{task.title[:60]}» не стартовала: разговор "
                        "OpenHands завис в loading (начальное сообщение "
                        "не обработано)."
                    )
                return

            # Агент возобновил работу после ответа/подталкивания — снимаем
            # защиту от повторного вопроса и повторного подталкивания.
            if state_val != "awaiting_user_input":
                answered_msg = None
                nudged_msg = None

            # Агент ждёт ввода. Пауза бывает трёх видов: вопрос (реально просит
            # ввода), финальный отчёт (работа сделана; модель завершает им
            # разговор с wait_for_response=true, не вызывая finish) и объявление
            # намерения («начну с изучения…» — ввода не просит). Путать отчёт с
            # вопросом нельзя: иначе оркестратор вешает на автора задачи
            # ожидание ответа на... собственный отчёт агента.
            if state_val == "awaiting_user_input":
                message = (info.get("message") or "").strip()
                # Прогресс между паузами: если с прошлой паузы агент выполнял
                # команды и вносил правки — счётчик авто-продолжений сбрасываем.
                # Иначе однажды достигнув лимита, он уже НЕ обнулялся, и каждое
                # промежуточное сообщение агента («теперь пишу тест…»)
                # навсегда пересылалось автору как «вопрос» — задача зависала,
                # пока автор не отвечал фейковыми «да» (issue #38).
                # Прогресс = рост РЕАЛЬНО исполненных команд с прошлой паузы
                # (get_events), а не work_events: OpenHands обнуляет
                # work_events при каждом запуске агента, поэтому сброс
                # срабатывал в каждом цикле «пауза → подталкивание» и
                # авто-продолжения становились бесконечными — лог спамился
                # «(1/4)» (разбор задачи #46, attempt 3).
                try:
                    pause_events = await self.openhands.get_events(
                        conversation_id
                    )
                except Exception:  # noqa: BLE001 — события недоступны, не падаем
                    pause_events = []
                cmd_count = sum(
                    1
                    for e in pause_events or []
                    if isinstance(e, dict) and e.get("action") == "run"
                )
                if last_pause_cmds is not None and cmd_count > last_pause_cmds:
                    auto_continues = 0
                last_pause_cmds = cmd_count
                # Сырой вызов инструмента текстом (<function=...>) — модель
                # выдала сломанный tool-calling: провайдер не разобрал вызов.
                # Единичные случаи лечатся подталкиванием, но серия таких пауз
                # БЕЗ ни одной исполненной команды за весь разговор — деградация
                # провайдера (задача #46, attempt 3: 17 сырых вызовов, 0 команд,
                # 4 часа бесполезных подталкиваний). Роняем задачу сразу.
                if "<function=" in message:
                    raw_tool_pauses += 1
                else:
                    raw_tool_pauses = 0
                if raw_tool_pauses >= self._RAW_TOOL_PAUSE_LIMIT and cmd_count == 0:
                    task.status = TaskStatus.FAILED
                    task.add_log(
                        "LLM-провайдер отдаёт сломанный tool-calling: "
                        f"{raw_tool_pauses} пауз подряд с сырым вызовом "
                        f"инструмента текстом, исполненных команд: 0"
                    )
                    await self._finalize_cost(task)
                    self.state.save(task)
                    self._notify_author(
                        task,
                        "❌ Агент не смог выполнить ни одной команды: LLM-провайдер "
                        "отдаёт сломанный tool-calling (вызовы инструментов приходят "
                        "обычным текстом и не исполняются). Задача остановлена — "
                        "перезапусти её позже, обычно это временный сбой провайдера.",
                    )
                    if task.source == TaskSource.GITHUB:
                        self._notify_admin(
                            f"⚠️ LLM-провайдер ({self._effective_model(task)}): "
                            "сломанный tool-calling — агент не исполнил ни одной "
                            f"команды. Задача {self._repo_full(task)}#"
                            f"{task.source_id} «{task.title[:60]}» остановлена."
                        )
                    return
                kind = self._classify_agent_pause(message)

                if kind == "report":
                    # Отчёт о проделанной работе — не вопрос, ждать ответа
                    # не нужно: завершаем задачу штатно (вплоть до PR).
                    # Саммари сохраняем: уйдёт отдельным сообщением перед
                    # коротким уведомлением со ссылками.
                    task.agent_summary = self._strip_tool_markup(message)[:4000]
                    task.status = TaskStatus.DONE
                    task.add_log("OpenHands completed (final report, no question)")
                    self.state.save(task)
                    await self._build_ios_and_create_pr(task, client_dir)
                    return

                # Вопрос автору нужен, если это настоящий вопрос либо если
                # авто-продолжения исчерпаны (объявление без ответа автора
                # иначе повиснет навсегда).
                escalate = (
                    kind == "question" or auto_continues >= self.max_auto_continues
                )
                if escalate:
                    if message and message == answered_msg:
                        # Вопрос только что задан/отвечен, агент ещё не проснулся
                        # — повторный вопрос по тому же тексту задавать нельзя.
                        await asyncio.sleep(self.config.clarification_poll_seconds)
                        continue
                    # Вопрос агента в процессе работы: показываем, ждём ответ,
                    # отправляем в тот же разговор и продолжаем мониторинг.
                    answered = await self._handle_agent_question(
                        task, conversation_id, message
                    )
                    if task.status == TaskStatus.CANCELLED:
                        return
                    if not answered:
                        task.status = TaskStatus.FAILED
                        task.add_log(
                            "Не удалось получить или доставить ответ на вопрос агента"
                        )
                        await self._finalize_cost(task)
                        self.state.save(task)
                        self._notify_author(
                            task,
                            "⏰ Не дождался ответа на вопрос — задача остановлена.\n"
                            f"Вопрос был: {message[:200]}",
                        )
                        return
                    answered_msg = message
                    continue

                # Объявление намерения без вопроса: подталкиваем продолжать.
                if message and message == nudged_msg:
                    # Подталкивание уже отправлено, агент ещё не отработал его.
                    await asyncio.sleep(self.config.clarification_poll_seconds)
                    continue
                auto_continues += 1
                task.add_log(
                    "Агент на паузе без вопроса — авто-продолжение "
                    f"({auto_continues}/{self.max_auto_continues})"
                )
                self.state.save(task)
                res = await self.openhands.send_message(
                    conversation_id, self._CONTINUE_NUDGE
                )
                if res.get("status") == "error" or res.get("success") is False:
                    # Доставка не прошла (возможно, разговор погас) — будим
                    # и пробуем ещё раз.
                    await self.openhands.start_conversation(conversation_id)
                    await self.openhands.send_message(
                        conversation_id, self._CONTINUE_NUDGE
                    )
                nudged_msg = message
                await asyncio.sleep(self.config.clarification_poll_seconds)
                continue

            if state_val == "finished":
                task.status = TaskStatus.DONE
                self.state.save(task)
                task.add_log("OpenHands finished")
                self.state.save(task)
                await self._build_ios_and_create_pr(task, client_dir)
                return

            if state_val in ("error", "rejected"):
                error_msg = (info.get("message") or f"агент в состоянии {state_val}")
                reason = info.get("error_reason") or ""
                stuck_killed = self._is_stuck_loop_error(reason)
                if state_val == "error" and stuck_killed and stuck_retries < 1:
                    # OpenHands сам остановил агента (зацикливание). Один ретрай:
                    # свежий старт разговора — обычно модель не повторяет цикл.
                    stuck_retries += 1
                    task.add_log(
                        "OpenHands остановил агента: зацикливание "
                        "(AgentStuckInLoopError) — перезапускаю разговор "
                        "(попытка 1/1)"
                    )
                    self.state.save(task)
                    self._notify_author(
                        task,
                        "🪓 OpenHands остановил агента: обнаружено зацикливание.\n"
                        "Перезапускаю разговор (попытка 1/1) — работа в контейнере "
                        "сохранена, продолжу с того же места.",
                    )
                    await asyncio.sleep(self.error_retry_wait_seconds)
                    await self.openhands.start_conversation(conversation_id)
                    await self.openhands.send_message(
                        conversation_id, self._CONTINUE_NUDGE
                    )
                    await asyncio.sleep(self.config.clarification_poll_seconds)
                    continue
                if state_val == "error" and self._is_transient_agent_error(reason):
                    if error_retries >= self.max_error_retries:
                        task.status = TaskStatus.FAILED
                        task.add_log(
                            "OpenHands failed (temporary error): "
                            f"{reason or error_msg} — attempts exhausted "
                            f"({self.max_error_retries})"
                        )
                        await self._finalize_cost(task)
                        self.state.save(task)
                        log_text = "\n".join(task.log[-15:]) if task.log else ""
                        self._notify_author(
                            task,
                            f"❌ OpenHands завершился с ошибкой.\n\n"
                            f"Ошибка: {reason or error_msg}\n\n"
                            f"Временная ошибка не устранена за "
                            f"{self.max_error_retries} попыток. "
                            f"Лог выполнения:\n{log_text}",
                        )
                        return
                    error_retries += 1
                    task.add_log(
                        f"OpenHands temporary error: {reason or error_msg} — "
                        f"retrying in {self.error_retry_wait_seconds}s "
                        f"({error_retries}/{self.max_error_retries})"
                    )
                    self.state.save(task)
                    await asyncio.sleep(self.error_retry_wait_seconds)
                    await self.openhands.start_conversation(conversation_id)
                    await self.openhands.send_message(
                        conversation_id, self._CONTINUE_NUDGE
                    )
                    await asyncio.sleep(self.config.clarification_poll_seconds)
                    continue
                task.status = TaskStatus.FAILED
                if stuck_killed:
                    task.add_log(
                        "OpenHands killed task: агент зациклился "
                        "(AgentStuckInLoopError) — ретрай не помог"
                    )
                else:
                    task.add_log(f"OpenHands failed: {error_msg}")
                await self._finalize_cost(task)
                self.state.save(task)
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                if stuck_killed:
                    # OpenHands сам убил задачу — пишем это явно и автору, и
                    # в issue (для GH-задач _notify_author = комментарий), и
                    # админу отдельным уведомлением.
                    kill_text = (
                        "🪓 OpenHands остановил и убил задачу: агент зациклился "
                        "(AgentStuckInLoopError).\n\n"
                        "Ретрай с перезапуском разговора не помог. Написанная "
                        "работа сохранена в контейнере; после перезапуска задачи "
                        "можно продолжить с того же места или изменить формулировку "
                        "задачи, чтобы выйти из цикла.\n\n"
                        f"Лог выполнения:\n{log_text}"
                    )
                    self._notify_author(task, kill_text)
                    if task.source == TaskSource.GITHUB:
                        self._notify_admin(
                            f"🪓 Задача из GitHub убита OpenHands "
                            f"(зацикливание агента):\n"
                            f"{self._repo_full(task)}#"
                            f"{task.source_id} «{task.title[:60]}»"
                        )
                else:
                    self._notify_author(
                        task,
                        f"❌ OpenHands завершился с ошибкой.\n\n"
                        f"Ошибка: {error_msg}\n\n"
                        f"Лог выполнения:\n{log_text}",
                    )
                return

            # stopped — разговор погашен. Причины разные: агент уже отчитался
            # (штатно), разговор погас во время ожидания ответа пользователя
            # (надо разбудить и продолжить), либо задачу оборвали.
            if state_val == "stopped":
                # Остановка после отчёта агента — штатное завершение
                # (OpenHands гасит простаивающий разговор).
                if info.get("completed"):
                    # Саммари — последний отчёт агента (если ещё не сохранён).
                    if not task.agent_summary and info.get("message"):
                        task.agent_summary = self._strip_tool_markup(
                            str(info["message"])
                        )[:4000]
                    task.status = TaskStatus.DONE
                    task.add_log("OpenHands completed (conversation stopped after report)")
                    self.state.save(task)
                    await self._build_ios_and_create_pr(task, client_dir)
                    return
                if info.get("state_before_stop") == "awaiting_user_input":
                    # Разговор погас (idle-таймаут), пока агент ждал ответ.
                    # Будим разговор: вопрос остался в траектории, монитор
                    # задаст его заново; уже данный ответ доставит поллер.
                    stop_restarts += 1
                    if stop_restarts > self.max_stop_restarts:
                        task.status = TaskStatus.FAILED
                        task.add_log(
                            "OpenHands: разговор гаснет при ожидании ответа, "
                            f"попыток пробуждения исчерпано ({self.max_stop_restarts})"
                        )
                        await self._finalize_cost(task)
                        self.state.save(task)
                        self._notify_author(
                            task,
                            "❌ Разговор с агентом остановлен во время ожидания "
                            "ответа и не возобновляется. Задача остановлена.",
                        )
                        return
                    task.add_log(
                        "OpenHands: разговор остановлен во время ожидания "
                        f"ответа — возобновляю (попытка {stop_restarts})"
                    )
                    self.state.save(task)
                    await self.openhands.start_conversation(conversation_id)
                    await asyncio.sleep(self.config.clarification_poll_seconds)
                    continue
                task.status = TaskStatus.FAILED
                task.add_log("OpenHands stopped before completion")
                await self._finalize_cost(task)
                self.state.save(task)
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                self._notify_author(
                    task,
                    f"❌ OpenHands остановлен до завершения задачи.\n\n"
                    f"Лог выполнения:\n{log_text}",
                )
                return

            await asyncio.sleep(self.config.clarification_poll_seconds)

    # Признаки временной ошибки агента (недоступность LLM-провайдера):
    # связаны с сетью/квотами/доступностью — сами проходят со временем.
    _TRANSIENT_ERROR_MARKERS = (
        "ServiceUnavailable", "Service Unavailable",
        "temporarily unavailable", "temporarily_unavailable",
        "overloaded", "rate limit", "RateLimit", "RateLimitError",
        "429", "503", "502", "504", "500",
        "bad gateway", "Bad Gateway", "internal server error",
        "timeout", "timed out", "Timeout", "TimeoutError",
        "connection error", "Connection error", "ConnectionError",
        "APIConnectionError", "connect", "unreachable", "reset by peer",
    )

    @classmethod
    def _is_transient_agent_error(cls, reason: str) -> bool:
        """Временная ли это ошибка агента (по тексту причины из OpenHands)."""
        if not reason:
            return False
        lowered = reason.lower()
        return any(marker.lower() in lowered for marker in cls._TRANSIENT_ERROR_MARKERS)

    @staticmethod
    def _is_stuck_loop_error(reason: str) -> bool:
        """OpenHands сам остановил агента за зацикливание?"""
        lowered = (reason or "").lower()
        return "agentstuckinloop" in lowered or "stuck in a loop" in lowered

    @staticmethod
    def _killed_by_openhands(task: Task) -> bool:
        """Задача была убита OpenHands и НЕ перезапущена после этого.

        След ищем в логе задачи, но с учётом попыток: маркер смерти,
        найденный ПОСЛЕ последнего «Перезапуск #N», относится к прошлой
        попытке и статус текущей не описывает (иначе после перезапуска
        задача навсегда показывалась как 🪓-убитая, хотя жива и работает).
        """
        last_restart = -1
        for i, entry in enumerate(task.log):
            if "Перезапуск #" in entry:
                last_restart = i
        return any(
            i > last_restart
            and (
                "AgentStuckInLoopError" in entry
                or "OpenHands killed task" in entry
            )
            for i, entry in enumerate(task.log)
        )

    # Серия пауз с сырым вызовом инструмента текстом (<function=...) без ни
    # одной исполненной команды = сломанный tool-calling у LLM-провайдера.
    # Подталкиванием не лечится — роняем задачу, чтобы не жечь часы и токены.
    _RAW_TOOL_PAUSE_LIMIT = 3

    # Подталкивание агенту, вставшему на паузу без вопроса (объявил намерение
    # и замолчал): даём ему ясный протокол — работать, спросить или отчитаться.
    _CONTINUE_NUDGE = (
        "Продолжай выполнение задачи по исходной инструкции. "
        "Если ты сообщил промежуточный статус — просто продолжай работу. "
        "Если тебе не хватает информации для продолжения — задай конкретный "
        "вопрос со знаком вопроса (при уместности — с пронумерованными "
        "вариантами ответа). "
        "Если работа полностью завершена — ответь «работа завершена» и "
        "перечисли сделанные изменения."
    )

    # Явные просьбы ввода: агент спрашивает, а не констатирует.
    _ASK_MARKERS = (
        # ru
        "уточн", "подтверд", "выберите", "выбери", "выбрать", "предпочита",
        "какой вариант", "какой из", "какую", "каких", "какой формат",
        "нужно ли", "стоит ли", "не хватает", "недостаточно данных",
        "недостаточно информации", "скажите", "подскажи", "сообщите",
        "ответьте", "укажите", "укажи", "ваш выбор", "твой выбор",
        # en
        "please confirm", "please specify", "please clarify", "please choose",
        "let me know", "which option", "which would", "your preference",
        "do you want", "would you like", "should i", "shall i", "clarif",
        "answer with", "reply with", "need your input", "waiting for your",
    )

    # Признаки финального отчёта о проделанной работе.
    _REPORT_MARKERS = (
        "что сделано", "что было сделано", "все проверки", "проверки пройдены",
        "проверки прошли", "тесты пройдены", "тесты прошли", "все тесты",
        "готово", "работа завершена", "задача выполнена", "завершена",
        "завершено", "итог", "резюме", "сводка", "результаты проверки",
        "изменения внесены", "рабочее дерево чистое",
        "summary", "all checks passed", "successfully completed",
        "working tree clean",
    )

    # Фразы, указывающие, что агент собирается продолжать: пауза с ними —
    # не финальный отчёт, даже если в тексте есть слова из _REPORT_MARKERS
    # (например, «Тесты прошли. Теперь запускаю сборку APK»).
    _FUTURE_MARKERS = (
        "теперь", "далее", "затем", "сейчас", "следующий шаг", "продолжу",
        "продолжаю", "запускаю", "приступаю", "начну", "начинаю", "сначала",
        "осталось",
        "now i", "next step", "let me", "going to", "i'll", "i will",
    )

    # Маркеры будущего сопоставляем ПО ГРАНИЦАМ СЛОВ: простое вхождение
    # подстроки даёт ложные срабатывания — «запускаю» совпадает внутри
    # «тесты не запускаются», и финальный отчёт классифицируется как
    # объявление намерения. Тогда монитор бесконечно подталкивает агента,
    # а тот в ответ снова шлёт тот же отчёт (зависание задачи #5).
    _FUTURE_RE = re.compile(
        r"\b(?:" + "|".join(re.escape(m) for m in _FUTURE_MARKERS) + r")\b"
    )

    _TOOL_CALL_RE = re.compile(r"<function=[^>]*>.*?(?:</function>|$)", re.DOTALL)

    # Служебные сообщения агента, дублирующие команды/мысли — в реплики
    # для Telegram их не показываем (команды видны отдельно как $ ...).
    _AGENT_NOISE_RE = re.compile(
        r"^(?:Running command:|Command `.*` executed|I am thinking|"
        r"Your thought has been logged|No observation|Code executed)"
    )

    @classmethod
    def _strip_tool_markup(cls, message: str) -> str:
        """Убрать из сообщения агента разметку вызовов инструментов.

        Модель иногда печатает вызов инструмента обычным текстом внутри
        сообщения (`<function=str_replace_editor>...`). Такой текст — не
        обращение к пользователю, а в коде внутри разметки встречаются
        вопросительные фразы ("which would break current clients"),
        которые ломали классификацию: объявление намерения считалось
        вопросом, задача уходила в ожидание ответа на два суток (issue #43).
        """
        return cls._TOOL_CALL_RE.sub(" ", message).strip()

    @classmethod
    def _classify_agent_pause(cls, message: str) -> str:
        """Классифицировать паузу агента в awaiting_user_input.

        Возвращает одно из:
        - "question" — агент реально просит ввода (вопрос, просьба уточнить);
        - "report" — финальный отчёт о проделанной работе (ждать ответа не надо);
        - "announce" — промежуточное сообщение/объявление намерения
          («начну с изучения файлов»), ввода не просит.
        """
        message = cls._strip_tool_markup(message)
        if not message:
            return "announce"
        lowered = message.lower()
        if message.rstrip().endswith("?") or any(
            m in lowered for m in cls._ASK_MARKERS
        ):
            return "question"
        if any(m in lowered for m in cls._REPORT_MARKERS) and not cls._FUTURE_RE.search(
            lowered
        ):
            return "report"
        return "announce"

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

        # Секреты сборки наружу не тащим: на хосте они уже лежат в secrets_dir,
        # а в PR они всё равно исключены из git add (страховка в глубину).
        secret_names = set(self._SECRET_FILE_NAMES)
        changed = [
            p for p in changed if os.path.basename(p.rstrip("/")) not in secret_names
        ]
        deleted = [
            p for p in deleted if os.path.basename(p.rstrip("/")) not in secret_names
        ]

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

    @staticmethod
    def _build_pr_body(task: Task, branch: str, assignees: list[str]) -> str:
        """Тело PR: описание, ветка, линковка с issue и упоминания.

        `Closes #N` (только для задач из GitHub) делает две вещи: PR
        прилинковывается к issue (секция Development) и issue автоматически
        закрывается при влитии PR в дефолтную ветку.
        """
        parts = [
            f"Автосоздано personal_worker для задачи:\n\n{task.description}\n",
            f"Источник: {task.source.value}",
            f"Ветка: `{branch}`",
        ]
        if task.source == TaskSource.GITHUB:
            parts.append(f"Closes #{task.source_id}")
        if assignees:
            parts.append("Ревьюеры: " + " ".join(f"@{a}" for a in assignees))
        parts.append(
            "Сборки и тесты пройдены агентом (Linux/Web/Android в контейнере, "
            "iOS на iMac по SSH)."
        )
        return "\n".join(parts)

    async def _build_ios_and_create_pr(
        self, task: Task, client_dir: str, nudged: bool = False
    ) -> None:
        # Считаем стоимость отработанной модели до итоговых действий.
        await self._finalize_cost(task)
        # Сначала вытаскиваем правки агента из runtime-контейнера на хост,
        # иначе в PR не попадёт ничего.
        sync = await self._sync_changes_from_runtime(task, client_dir)
        if sync.get("status") == "ok":
            task.add_log(
                f"Из контейнера: изменено {sync.get('copied')}, "
                f"удалено {sync.get('removed')}"
            )
        elif sync.get("status") == "error":
            # При подталкивании runtime-контейнер может быть уже погашен,
            # а коммит с правками агента — уже лежать в рабочей копии
            # (упали позже, на пуше). Тогда синхронизация не нужна:
            # пушим то, что есть. Иначе — честная ошибка.
            ahead = await self._git_ahead_count(client_dir)
            if nudged and ahead > 0:
                task.add_log(
                    f"Синхронизация из контейнера пропущена "
                    f"({sync.get('error')}) — коммит с правками уже в рабочей копии"
                )
            else:
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

        # iOS на iMac НЕ собираем здесь: агент сам проверил iOS-сборку по SSH
        # из runtime-контейнера (см. инструкцию в _run_openhands_impl) и
        # интерпретировал результат. Дублирующая сборка оркестратором излишня.

        # Создаём ветку, коммит, пуш, PR. Ветка зависит от номера попытки —
        # чтобы перезапуск не конфликтовал с веткой прошлого прогона.
        branch = f"ai-task-{task.id}-r{task.attempt}"
        dry_run = self.config.pr_dry_run
        try:
            for git_cmd in (
                ["checkout", "-b", branch],
                # Секреты сборки (google-services.json, build.env) кладутся в
                # рабочую копию ради сборки, но в PR уезжать не должны.
                # exclude,glob — обязательно: литеральный путь игнорируемого
                # файла (google-services.json есть в .gitignore) валит git add
                # с "paths are ignored" (exit 1). **/ ловит и корень, и вложенность.
                [
                    "add", "-A", "--", ".",
                    ":(exclude,glob)**/build.env",
                    ":(exclude,glob)**/google-services.json",
                ],
                ["commit", "-m", f"AI task: {task.title}"],
                # В dry-run пуш только проверяется — на GitHub ничего не уходит.
                (["push", "--dry-run", "-u", "origin", branch] if dry_run
                 else ["push", "-u", "origin", branch]),
            ):
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", client_dir, *self._git_auth_args(task.repo), *git_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    # Пустой коммит — не ошибка, если правок и вправду нет.
                    # ВАЖНО: git commit печатает "nothing to commit" в STDOUT,
                    # а не в stderr — проверять нужно оба потока, иначе задача
                    # без правок падает с пустой ошибкой.
                    msg = err.decode("utf-8", errors="replace").strip()
                    out_msg = out.decode("utf-8", errors="replace")
                    if git_cmd[0] == "commit" and (
                        "nothing to commit" in msg or "nothing to commit" in out_msg
                    ):
                        task.add_log("git: нет изменений для коммита")
                        continue
                    shown = msg or out_msg.strip()
                    raise RuntimeError(f"git {' '.join(git_cmd)}: {shown}")
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
                f"Стоимость: {self._cost_line(task)}\n\n"
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
                f"Ветка: {branch} (push --dry-run прошёл)\n"
                f"Стоимость: {self._cost_line(task)}\n\n"
                f"Изменения:\n{diff}\n\n"
                f"Лог выполнения:\n" + "\n".join(task.log[-10:]),
            )
            return

        # Assignees issue → ревьюеры PR + упоминания. Только для задач из
        # GitHub (у Telegram-задач issue нет). Сбой здесь не валит задачу:
        # PR важнее ревьюеров.
        assignees: list[str] = []
        if task.source == TaskSource.GITHUB:
            try:
                assignees = self.github_for(task.repo).get_issue_assignees(int(task.source_id))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[github] assignees fetch failed: %s", exc)

        pr_body = self._build_pr_body(task, branch, assignees)
        pr_url = self.github_for(task.repo).create_pr(
            branch=branch,
            base="main",
            title=f"[AI] {task.title}",
            body=pr_body,
        )
        task.pr_url = pr_url
        task.status = TaskStatus.DONE
        self.state.save(task)

        if assignees:
            try:
                self.github_for(task.repo).request_reviewers(pr_url, assignees)
                task.add_log(f"Ревьюеры запрошены: {', '.join(assignees)}")
            except Exception as exc:  # noqa: BLE001 — PR уже создан, не валить
                logger.warning("[github] reviewers request failed: %s", exc)
                task.add_log(f"Не удалось запросить ревьюеров: {exc}")
                self.state.save(task)

        # Артефакты GHA: имена берём из workflow на нашей ветке, запуск ищем
        # по sha коммита (триггер срабатывает с лагом — поллим недолго).
        # Прямые ссылки на файлы появятся после сборки — следим в фоне и
        # редактируем свой комментарий между маркерами.
        artifacts_section, run_id = await self._gha_artifacts_section(
            task, client_dir, branch
        )
        # Финальное саммари — отдельным сообщением ДО короткого уведомления
        # со ссылками: полноценный отчёт «что сделано» в духе ревью, а потом
        # сухие ссылки. Сбой саммари не валит задачу: уведомление важнее.
        summary = (task.agent_summary or "").strip()
        if summary:
            try:
                await self._notify_summary(task, summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[summary] failed for %s: %s", task.id, exc)
        notify_text = f"✅ Задача выполнена! PR создан: {pr_url}\n"
        notify_text += f"Ветка: `{branch}`\n"
        if assignees:
            notify_text += (
                "Ревьюеры: " + " ".join(f"@{a}" for a in assignees) + "\n"
            )
        notify_text += f"Стоимость: {self._cost_line(task)}\n"
        if artifacts_section:
            notify_text += f"\n{artifacts_section}"
        comment_id = await self._notify_author_with_comment_id(task, notify_text)
        if run_id and comment_id:
            self._spawn(
                self._watch_gha_artifacts(
                    task, comment_id, notify_text, run_id, client_dir, branch
                )
            )

    # ------------------------------------------------------------------ #
    # Артефакты GitHub Actions в финальном уведомлении
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_workflow_artifacts(client_dir: str) -> list[str]:
        """Имена артефактов (actions/upload-artifact) из workflow на ветке.

        Читаем локальные .github/workflows/*.yml рабочей копии — там ровно та
        версия workflow, с которой соберётся наш PR. Имена с выражениями
        ({{ ... }}) пропускаем: заранее их не вычислить.
        """
        wf_dir = os.path.join(client_dir, ".github", "workflows")
        if not os.path.isdir(wf_dir):
            return []
        names: list[str] = []
        try:
            files = sorted(
                f for f in os.listdir(wf_dir)
                if f.endswith((".yml", ".yaml"))
            )
        except OSError:
            return []
        for fn in files:
            try:
                with open(os.path.join(wf_dir, fn), encoding="utf-8") as f:
                    doc = yaml.safe_load(f)
            except Exception:  # noqa: BLE001 — битый/не-yaml файл пропускаем
                continue
            if not isinstance(doc, dict):
                continue
            for job in (doc.get("jobs") or {}).values():
                if not isinstance(job, dict):
                    continue
                for step in job.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    uses = str(step.get("uses") or "")
                    if not uses.startswith("actions/upload-artifact"):
                        continue
                    name = (step.get("with") or {}).get("name")
                    if isinstance(name, str) and name and "${{" not in name:
                        if name not in names:
                            names.append(name)
        return names

    async def _git_head_sha(self, client_dir: str) -> Optional[str]:
        """sha коммита, который отправили на GitHub (для поиска запуска)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", client_dir, "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            sha = out.decode().strip()
            return sha if proc.returncode == 0 and sha else None
        except Exception:  # noqa: BLE001 — не критично для уведомления
            return None

    async def _gha_artifacts_section(
        self, task: Task, client_dir: str, branch: str
    ) -> tuple[str, Optional[int]]:
        """Секция «Артефакты GHA» для финального уведомления + id запуска.

        Запуск ищем по sha коммита недолго (до ~40 с): он нужен для ссылки
        на прогресс сборки и для фонового дописывания прямых ссылок. Если
        запуск не успел появиться — даём ссылку на проверки коммита, она
        стабильна. Возвращаем ("", None), если артефактов в workflow нет.
        """
        names = self.parse_workflow_artifacts(client_dir)
        if not names:
            return "", None

        sha = await self._git_head_sha(client_dir)
        run = None
        if sha:
            try:
                run = await asyncio.to_thread(
                    self.github_for(task.repo).find_workflow_run, sha, 40, 8.0
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[gha] find_workflow_run failed: %s", exc)

        lines = ["📦 Артефакты GHA (собираются в CI):"]
        lines += [f"  • {n}" for n in names]
        if run and run.get("html_url"):
            lines.append(f"🔧 Сборка: {run['html_url']}")
            lines.append(
                "_(прямые ссылки на файлы допишу сюда после завершения сборки)_"
            )
        elif sha:
            checks = (
                f"https://github.com/{self._repo_full(task)}"
                f"/commit/{sha}/checks"
            )
            lines.append(f"🔧 Сборка: {checks}")
        body = "\n".join(lines)
        section = f"<!-- gha-artifacts-start -->\n{body}\n<!-- gha-artifacts-end -->"
        task.add_log(
            f"Артефакты GHA: {', '.join(names)}"
            + (f"; run {run['id']}" if run else "; запуск не найден")
        )
        self.state.save(task)
        return section, (run or {}).get("id")

    @staticmethod
    def replace_marker_section(body: str, new_section: str) -> str:
        """Заменить текст между маркерами gha-artifacts-* на новый."""
        start = body.find("<!-- gha-artifacts-start -->")
        end = body.find("<!-- gha-artifacts-end -->")
        if start == -1 or end == -1 or end < start:
            return body
        return (
            body[:start]
            + new_section.strip()
            + "\n"
            + body[end + len("<!-- gha-artifacts-end -->"):]
        )

    @staticmethod
    def format_artifacts_section(
        run_id: int,
        run_url: str,
        conclusion: Optional[str],
        artifacts: list[dict],
        repo_full_name: str,
    ) -> str:
        """Финальная секция артефактов: прямые ссылки + размер или вердикт."""
        if conclusion == "success" and artifacts:
            lines = ["📦 Артефакты GHA (готовы):"]
            for a in artifacts:
                url = (
                    f"https://github.com/{repo_full_name}"
                    f"/actions/runs/{run_id}/artifacts/{a['id']}"
                )
                mb = a["size_in_bytes"] / 1_000_000
                size = f"{mb:.1f} МБ" if mb < 1000 else f"{mb / 1000:.2f} ГБ"
                lines.append(f"  • [{a['name']}]({url}) — {size}")
            lines.append(f"🔧 Сборка: {run_url} ✅")
        elif conclusion == "success":
            lines = [
                "📦 Артефакты GHA: сборка успешна, артефактов нет.",
                f"🔧 Сборка: {run_url}",
            ]
        else:
            lines = [
                f"📦 Артефакты GHA: сборка завершилась ({conclusion or 'неизвестно'}).",
                f"🔧 Сборка: {run_url}",
            ]
        return (
            "<!-- gha-artifacts-start -->\n"
            + "\n".join(lines)
            + "\n<!-- gha-artifacts-end -->"
        )

    async def _watch_gha_artifacts(
        self, task: Task, comment_id: int, comment_body: str, run_id: int,
        client_dir: Optional[str] = None, branch: Optional[str] = None,
    ) -> None:
        """Дождаться завершения сборки GHA и дописать прямые ссылки.

        Редактируем свой финальный комментарий: секция между маркерами
        gha-artifacts-* заменяется на версию с прямыми ссылками на файлы.
        Ограничены по времени (GHA_WATCH_TIMEOUT_SECONDS), чтобы не висеть
        вечно на зависшей сборке.

        Провал сборки — не конец: логи упавших джоб отправляются агенту в
        тот же разговор, он чинит и допушивает в ветку PR, вотчер следит за
        новым прогоном (до GHA_FIX_MAX_ROUNDS раундов).

        Пока вотчер жив, в state/gha_watchers/ лежит маркер <run_id>.watch:
        деплой по нему ждёт окончания «хвоста» перед остановкой сервиса.
        """
        watchers_dir = Path(self.config.state_db).parent / "gha_watchers"
        marker = watchers_dir / f"{run_id}.watch"
        try:
            watchers_dir.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            logger.warning("[gha] не удалось создать маркер %s", marker)
        try:
            await self._watch_gha_artifacts_loop(
                task, comment_id, comment_body, run_id, client_dir, branch
            )
        finally:
            marker.unlink(missing_ok=True)

    async def _watch_gha_artifacts_loop(
        self, task: Task, comment_id: int, comment_body: str, run_id: int,
        client_dir: Optional[str] = None, branch: Optional[str] = None,
    ) -> None:
        old_sha = None
        if client_dir:
            try:
                old_sha = await self._git_head_sha(client_dir)
            except Exception:  # noqa: BLE001 — sha нужен только для ретрая
                old_sha = None
        fix_rounds = 0
        while True:
            conclusion = await self._wait_ci_run_completion(task, run_id)
            run_url = (
                f"https://github.com/{self._repo_full(task)}"
                f"/actions/runs/{run_id}"
            )
            if conclusion is None:
                # Таймаут слежения: сборка так и не завершилась.
                task.add_log(
                    "Сборка GHA не завершилась за время слежения — "
                    "ссылки не дописаны"
                )
                self.state.save(task)
                self._notify_author(
                    task,
                    "⏳ Сборка GHA не завершилась за время слежения "
                    f"({self.gha_watch_timeout_seconds // 60} мин) — "
                    "проверь её статус вручную:\n"
                    f"{run_url}\n\n"
                    "PR и код задачи в порядке; артефакты могут появиться позже.",
                )
                if task.source == TaskSource.GITHUB:
                    self._notify_admin(
                        f"⏳ GHA-сборка задачи {self._repo_full(task)}"
                        f"#{task.source_id} не завершилась за время слежения:\n"
                        f"{run_url}"
                    )
                return
            # Сборка завершилась — обновляем секцию артефактов в комментарии.
            artifacts: list[dict] = []
            if conclusion == "success":
                try:
                    artifacts = await asyncio.to_thread(
                        self.github_for(task.repo).list_run_artifacts, run_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[gha] list_run_artifacts failed: %s", exc)
            new_section = self.format_artifacts_section(
                run_id, run_url, conclusion, artifacts, self._repo_full(task)
            )
            comment_body = self.replace_marker_section(comment_body, new_section)
            try:
                await asyncio.to_thread(
                    self.github_for(task.repo).update_comment, comment_id, comment_body
                )
            except Exception as exc:
                logger.error("[gha] update_comment failed: %s", exc)
                return
            task.add_log(
                f"Ссылки на артефакты GHA добавлены в комментарий "
                f"(сборка: {conclusion}, артефактов: {len(artifacts)})"
            )
            self.state.save(task)
            if conclusion == "success":
                return
            # Сборка провалилась. Раньше — только уведомление; теперь цикл
            # замкнут: логи упавших джоб → агенту → допуш → новый прогон.
            if fix_rounds >= self.max_ci_fix_rounds:
                task.add_log(
                    f"GHA-сборка провалилась ({conclusion}) — лимит раундов "
                    f"исправлений ({self.max_ci_fix_rounds}) исчерпан"
                )
                self.state.save(task)
                self._notify_author(
                    task,
                    f"❌ GHA-сборка снова провалилась ({conclusion}) после "
                    f"{self.max_ci_fix_rounds} раундов автоисправлений.\n"
                    f"{run_url}\n\n"
                    "Разберись в логах вручную или перезапусти задачу.",
                )
                if task.source == TaskSource.GITHUB:
                    self._notify_admin(
                        f"❌ GHA-сборка задачи {self._repo_full(task)}"
                        f"#{task.source_id} не починена за "
                        f"{self.max_ci_fix_rounds} раунда: {conclusion}\n"
                        f"{run_url}"
                    )
                return
            fix_rounds += 1
            task.add_log(
                f"GHA-сборка провалилась ({conclusion}) — отправляю логи "
                f"агенту на исправление (раунд {fix_rounds}/"
                f"{self.max_ci_fix_rounds})"
            )
            self.state.save(task)
            sent = await self._send_ci_failure_to_agent(task, run_id)
            if not sent:
                self._notify_author(
                    task,
                    f"❌ GHA-сборка провалилась ({conclusion}), но передать "
                    "логи агенту не удалось (разговор закрыт?).\n"
                    f"{run_url}",
                )
                if task.source == TaskSource.GITHUB:
                    self._notify_admin(
                        f"❌ GHA-сборка {self._repo_full(task)}"
                        f"#{task.source_id}: {conclusion}; логи агенту "
                        "доставить не удалось."
                    )
                return
            # Агент поправил файлы в контейнере — пушит ОРКЕСТРАТОР:
            # синхронизируем правки на хост и допушиваем в ветку PR.
            new_run = await self._sync_and_push_ci_fix(
                task, client_dir, branch, old_sha
            )
            if not new_run:
                self._notify_author(
                    task,
                    "⏳ Агент получил логи падения, но новый запуск CI так и "
                    "не появился за время ожидания. Проверь PR и логи агента "
                    "вручную.",
                )
                if task.source == TaskSource.GITHUB:
                    self._notify_admin(
                        f"⏳ {self._repo_full(task)}#{task.source_id}: агент "
                        "не допушил исправление после падения CI."
                    )
                return
            task.add_log(
                f"Агент допушил исправление — новый прогон CI: "
                f"{new_run.get('html_url') or new_run.get('id')}"
            )
            self.state.save(task)
            run_id = new_run["id"]
            old_sha = new_run.get("head_sha") or old_sha

    async def _sync_and_push_ci_fix(
        self, task: Task, client_dir: Optional[str],
        branch: Optional[str], old_sha: Optional[str],
    ) -> Optional[dict]:
        """Забрать правки агента из контейнера и допушить в ветку PR.

        Агент не пушит сам (у него нет токена, и он работает в собственном
        клоне внутри контейнера) — пуш всегда делает оркестратор:
        синхронизация из контейнера на хост → commit → push → новый прогон.
        Возвращаем dict нового запуска CI или None (не дождались).
        """
        if not client_dir or not branch:
            return None
        # Ждём, пока агент закончит правки (разговор снова idle/finished)
        # — до 20 минут; обычно хватает пары минут.
        await self._wait_agent_settled(task)
        sync = await self._sync_changes_from_runtime(task, client_dir)
        if sync.get("status") == "error":
            logger.error("[gha-fix] sync failed: %s", sync.get("error"))
            return None
        task.add_log(
            f"Правки агента после падения CI: изменено {sync.get('copied')}, "
            f"удалено {sync.get('removed')}"
        )
        try:
            for git_cmd in (
                ["add", "-A", "--", ".",
                 ":(exclude,glob)**/build.env",
                 ":(exclude,glob)**/google-services.json"],
                ["commit", "-m", f"AI task: {task.title} (CI fix)"],
                ["push", "-u", "origin", branch],
            ):
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", client_dir, *self._git_auth_args(task.repo), *git_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    msg = (err.decode("utf-8", errors="replace").strip()
                           or out.decode("utf-8", errors="replace").strip())
                    if git_cmd[0] == "commit" and "nothing to commit" in msg:
                        task.add_log("git: агент ничего не изменил после логов CI")
                        self.state.save(task)
                        return None
                    raise RuntimeError(f"git {' '.join(git_cmd)}: {msg}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[gha-fix] push failed: %s", exc)
            task.add_log(f"Допуш после падения CI не удался: {exc}")
            self.state.save(task)
            return None
        return await self._wait_for_new_ci_run(task, client_dir, old_sha, 0)

    async def _wait_agent_settled(
        self, task: Task, max_wait_s: int = 1200
    ) -> bool:
        """Дождаться, пока агент закончит правки после наших логов.

        Пока разговор не вернулся в idle/finished/stopped/awaiting — агент
        ещё работает. True — успокоился, False — таймаут.
        """
        conversation_id = task.conversation_id
        if not conversation_id:
            return True
        settled_states = {"idle", "finished", "stopped", "awaiting_user_input",
                          "error"}
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(20)
            try:
                info = await self.openhands.get_agent_state(conversation_id)
            except Exception:  # noqa: BLE001
                continue
            state = info.get("agent_state")
            if state in settled_states:
                return True
        return False

    async def _wait_ci_run_completion(self, task: Task, run_id: int) -> Optional[str]:
        """Дождаться завершения прогона CI. conclusion или None (таймаут)."""
        deadline = time.monotonic() + self.gha_watch_timeout_seconds
        while True:
            await asyncio.sleep(self.gha_watch_poll_seconds)
            if time.monotonic() >= deadline:
                return None
            try:
                run = await asyncio.to_thread(
                    self.github_for(task.repo).get_workflow_run, run_id
                )
            except Exception as exc:  # noqa: BLE001 — сеть/API, попробуем снова
                logger.warning("[gha] get_workflow_run failed: %s", exc)
                continue
            if not run or run.get("status") != "completed":
                continue
            return run.get("conclusion")

    # Выводы упавших джоб — кандидаты на отправку агенту.
    _CI_FAILED_CONCLUSIONS = ("failure", "cancelled", "timed_out")

    async def _send_ci_failure_to_agent(self, task: Task, run_id: int) -> bool:
        """Отправить агенту логи упавших джоб прогона CI.

        True — сообщение доставлено в разговор; агенту предлагается
        исправить и допушить в ту же ветку PR.
        """
        conversation_id = task.conversation_id
        if not conversation_id:
            return False
        try:
            jobs = await asyncio.to_thread(
                self.github_for(task.repo).list_run_jobs, run_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[gha] list_run_jobs failed: %s", exc)
            jobs = []
        chunks: list[str] = []
        for job in jobs:
            if (job.get("conclusion") or "") not in self._CI_FAILED_CONCLUSIONS:
                continue
            logs = None
            try:
                logs = await asyncio.to_thread(
                    self.github_for(task.repo).get_job_logs, job["id"]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[gha] get_job_logs failed: %s", exc)
            chunks.append(self._ci_log_excerpt(
                job.get("name") or f"job-{job.get('id')}", logs or ""
            ))
        if not chunks:
            chunks.append(
                "(логи упавших джоб недоступны — посмотри run по ссылке выше)"
            )
        message = (
            "⚠️ CI на GitHub провалился для твоего PR. Ниже хвосты логов "
            "упавших джоб.\n\n"
            "Разберись в причине и исправь код или workflow в своём рабочем "
            "окружении (/workspace/social_mind). ПУШИТЬ НЕ НУЖНО: просто "
            "внеси правки (можно закоммитить) и сообщи, что готово. "
            "Оркестратор сам заберёт изменения и допушит в ветку PR — CI "
            "перезапустится, о результате сообщу отдельно. Если проблема "
            "вне твоего контроля — задай вопрос со знаком вопроса.\n\n"
            + "\n\n".join(chunks)
        )
        res = await self.openhands.send_message(conversation_id, message)
        if res.get("status") == "error" or res.get("success") is False:
            # Разговор мог погаснуть после завершения задачи — будим.
            await self.openhands.start_conversation(conversation_id)
            res = await self.openhands.send_message(conversation_id, message)
            if res.get("status") == "error" or res.get("success") is False:
                logger.error(
                    "[gha-fix] не удалось доставить логи агенту: %s", res
                )
                return False
        return True

    @staticmethod
    def _ci_log_excerpt(job_name: str, logs: str, max_chars: int = 6000) -> str:
        """Хвост лога джобы: вокруг первой ошибки или последние строки."""
        lines = logs.splitlines()
        if lines:
            err_idx = next(
                (i for i, ln in enumerate(lines) if "##[error]" in ln), None
            )
            if err_idx is not None:
                start = max(0, err_idx - 15)
                window = lines[start : err_idx + 45]
            else:
                window = lines[-50:]
            text = "\n".join(window).strip()
            if len(text) > max_chars:
                text = text[-max_chars:]
        else:
            text = "(лог пуст или недоступен)"
        return f"--- Джоба «{job_name}» (хвост лога) ---\n{text}"

    async def _wait_for_new_ci_run(
        self, task: Task, client_dir: Optional[str],
        old_sha: Optional[str], old_run_id: int,
    ) -> Optional[dict]:
        """Дождаться нового прогона CI после допуша оркестратора.

        Смотрим head-коммит рабочей копии на хосте: до пуша он не меняется.
        Новый прогон ищем по новому sha (до ci_new_run_wait_seconds).
        """
        if not client_dir:
            return None
        deadline = time.monotonic() + self.ci_new_run_wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(30)
            try:
                sha = await self._git_head_sha(client_dir)
                if not sha or sha == old_sha:
                    continue
                run = await asyncio.to_thread(
                    self.github_for(task.repo).find_workflow_run, sha, 10, 5.0
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[gha] new run poll failed: %s", exc)
                continue
            if run and run.get("id") and int(run["id"]) != int(old_run_id):
                return run
        return None

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #
    async def _recover_interrupted_tasks(self) -> None:
        """На старте: найти прерванные (сбой/перезагрузка) задачи и продолжить.

        Задача в NEW/READY/CLARIFYING/RUNNING остаётся «в воздухе», если сервис
        упал или машину перезагрузили во время работы (NEW — ожидала в очереди
        выполнения). Уведомляем автора и админа о сбое, затем продолжаем:
        переподключаемся к живому разговору OpenHands либо перезапускаем
        задачу. Всё встаёт в очередь и выполняется по одной.
        """
        interrupted: list[Task] = []
        for status in (
            TaskStatus.NEW,
            TaskStatus.READY,
            TaskStatus.CLARIFYING,
            TaskStatus.RUNNING,
        ):
            interrupted.extend(self.state.list_by_status(status))
        if not interrupted:
            return

        logger.warning("[recover] found %d interrupted task(s)", len(interrupted))
        for task in interrupted:
            try:
                self._notify_author(
                    task,
                    "⚠️ Сервис был перезапущен (сбой или перезагрузка машины).\n"
                    f"Задача «{task.title[:60]}» была прервана — продолжаю автоматически.",
                )
                self._notify_admin(
                    f"⚠️ Сбой/перезагрузка сервиса. Прерванная задача {task.id} "
                    f"«{task.title[:40]}» — продолжаю."
                )
                await self._resume_task(task)
            except Exception as exc:  # noqa: BLE001
                # Ошибка одной задачи не должна ронять всё восстановление.
                logger.error("[recover] task %s resume error: %s", task.id, exc)

    async def _resume_task(self, task: Task) -> None:
        """Продолжить прерванную задачу: восстановить контекст и встать в очередь.

        Вся фактическая работа (переподключение к разговору, перезапуск,
        ожидание уточнения) происходит в _process_task, когда до задачи
        дойдёт очередь — так восстановленные задачи тоже не выполняются
        параллельно.
        """
        if task.status == TaskStatus.CLARIFYING:
            # Ждали ответа пользователя — восстанавливаем запись ожидания
            # (ответ, пришедший до сбоя, продолжит задачу).
            record: dict = {"task": task, "future": None, "asked_at": task.created_at}
            if task.source == TaskSource.GITHUB:
                # Без github_issue сбор ответов из комментариев вообще не работает,
                # а без after_comment_id старый комментарий сойдёт за ответ.
                try:
                    num = int(task.source_id)
                    record["github_issue"] = num
                    record["github_after_comment_id"] = await asyncio.to_thread(
                        self.github_for(task.repo).last_bot_comment_id, num
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[recover] %s: github clarify boundary restore failed: %s",
                        task.id, exc,
                    )
            self._pending_clarifications[task.id] = record
        self._enqueue_task(task)

    async def _refresh_prices_and_benchmarks(self) -> None:
        """Обновить цены и SWE-бенчмарки; сбой одного не мешает другому."""
        try:
            await pricing.refresh_prices(self.state, self.openhands)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop] price refresh error: %s", exc)
        try:
            await pricing.refresh_benchmarks(self.state)  # Vals AI поверх seed
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop] benchmark refresh error: %s", exc)

    async def run(self) -> None:
        logger.info("[scheduler] starting (poll_interval=%ds)", self.config.poll_interval_seconds)

        # Запуск Telegram polling с retry при сетевых сбоях — в ФОНЕ:
        # недоступный Telegram (таймауты api.telegram.org) не должен
        # блокировать восстановление задач — GitHub-issue задачи от него
        # не зависят, а встающие за ним _recover_interrupted_tasks подвешивали
        # всё выполнение до восстановления сети.
        self._spawn(self._start_telegram_with_retry())

        # Прерванные задачи (сбой/перезагрузка): уведомить и продолжить.
        try:
            await self._recover_interrupted_tasks()
        except Exception as exc:  # noqa: BLE001
            # Ошибка восстановления не должна блокировать старт сервиса.
            logger.error("[recover] interrupted-task recovery error: %s", exc)

        # Справочные метрики SWE и цены моделей: сид (fallback) + первое обновление.
        try:
            pricing.seed_benchmarks(self.state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[loop] seed benchmarks error: %s", exc)
        await self._refresh_prices_and_benchmarks()
        price_refresh_interval = 6 * 3600  # обновлять цены/бенчмарки раз в 6 часов
        last_price_refresh = time.time()
        last_container_gc = time.time()  # чистка runtime-контейнеров — раз в час

        while True:
            try:
                await self.poll_github()
            except Exception as exc:
                logger.error("[loop] github poll error: %s", exc)

            # Периодически чистим осиротевшие runtime-контейнеры.
            if time.time() - last_container_gc > 3600:
                try:
                    await self._gc_runtime_containers()
                except Exception as exc:
                    logger.error("[loop] container gc error: %s", exc)
                last_container_gc = time.time()

            # Периодически обновляем цены и бенчмарки моделей.
            if time.time() - last_price_refresh > price_refresh_interval:
                await self._refresh_prices_and_benchmarks()
                last_price_refresh = time.time()

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
