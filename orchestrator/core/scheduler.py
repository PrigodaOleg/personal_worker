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
import time
from datetime import datetime, timedelta
from typing import Optional

from orchestrator.adapters.github import GitHubAdapter
from orchestrator.adapters.imac import IMacAdapter
from orchestrator.adapters.openhands import OpenHandsAdapter
from orchestrator.adapters.telegram import MENU_COMMANDS, TelegramAdapter
from orchestrator.core.config import Config
from orchestrator.core import pricing
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
        self.imac = IMacAdapter(
            host=config.imac_host or "",
            user=config.imac_user or "",
            ssh_key=config.imac_ssh_key,
            work_dir=config.imac_work_dir,
        )
        self.ios_runner = IOSRunner(imac=self.imac)

        self._pending_clarifications: dict[str, dict] = {}
        # Ожидающие ответа пользователя ВОПРОСЫ АГЕНТА (в процессе работы):
        # task.id -> {task, conversation_id, options, future, asked_at}.
        self._pending_agent_questions: dict[str, dict] = {}
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
        # Сколько раз подряд можно авто-перезапускать прерванную задачу после
        # сбоя/перезагрузки, прежде чем пометить её failed (защита от зацикливания).
        self.max_resume_attempts: int = int(os.getenv("OPENHANDS_MAX_RESUME_ATTEMPTS", "3"))

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

    def _spawn(self, coro) -> None:
        """Запустить фоновую задачу, удерживая на неё ссылку.

        asyncio.create_task хранит только слабую ссылку: без удержания GC
        может оборвать задачу в середине выполнения.
        """
        fut = asyncio.ensure_future(coro)
        self._background_tasks.add(fut)
        fut.add_done_callback(self._background_tasks.discard)

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
        nav.append((f"📄 {page + 1}/{pages}", f"noop"))
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
            self._notify_author(
                task,
                f"🚀 Задача принята в работу: «{title[:60]}»\n"
                "Если понадобятся уточнения — спрошу здесь, в комментариях.",
            )
            self._spawn(self._process_task(task))

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
                comments = self.github.fetch_issue_comments(
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
                comments = self.github.fetch_issue_comments(
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
                    issue_state = self.github.fetch_issue_state(int(task.source_id))
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
            await self._handle_telegram_command(chat_id, text)
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
            self._spawn(self._process_task(task))
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
        self._spawn(self._process_task(task))

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

        Команды и их коды выхода из событий разговора (`/events`). Показываем и
        завершённые команды, и текущую (ещё выполняющуюся, помечена 🔄) — чтобы
        было видно, что агент делает прямо сейчас (например, идёт сборка APK).
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
                done.append(f"$ {self._short_cmd(cmd)}\n  → exit {code}")
                running = None

        rows = list(done[-lines:])
        if running:
            rows.append(f"🔄 {running} — выполняется…")
        if not rows:
            return "— агент ещё не выполнял команд"
        return "\n".join(rows)

    @staticmethod
    def _short_cmd(cmd: str, max_len: int = 100) -> str:
        cmd = (cmd or "").strip().replace("\n", " ⏎ ")
        return cmd if len(cmd) <= max_len else cmd[: max_len - 1] + "…"

    async def _handle_telegram_command(self, chat_id: str, text: str) -> None:
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
                "📊 Статус — состояние текущей задачи\n"
                "📋 Задачи — все задачи, результаты и ссылки на PR\n"
                "🧠 Модель — модель LLM по умолчанию\n"
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

        await self.telegram.send_message(
            chat_id_int,
            "❓ Неизвестная команда. Используйте /help для справки.",
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
        return self.telegram.build_keyboard([
            [("🔄 Обновить", f"refresh:{task.id}")],
            [("🔁 Перезапустить", f"restart:{task.id}"),
             ("🧠 Модель", f"model:{task.id}")],
            [("📖 Логи", f"logs:{task.id}"),
             ("📋 К списку", "tasks:0")],
            [("🗑 Удалить", f"delask:{task.id}")],
        ])

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
        layers = await self._layer_status_block(task)
        activity = await self._collect_task_logs(task, lines=6)
        text = (
            f"{emoji} Задача: «{task.title[:60]}»\n"
            f"Статус: {self._task_status_label(task.status)}\n"
            f"Модель: {self._effective_model(task)}\n"
            f"Стоимость: {self._cost_line(task)}\n"
            f"PR: {task.pr_url or '—'}\n\n"
            f"📊 Слои:\n{layers}\n\n"
            f"📋 Что делает агент:\n{activity}"
        )
        return text, self._task_actions_keyboard(task)

    def _task_card(self, task: Task) -> str:
        emoji = self._task_status_emoji(task.status)
        pr = task.pr_url or "—"
        model = self._effective_model(task)
        created = task.created_at.strftime("%Y-%m-%d %H:%M")
        src = "GitHub" if task.source == TaskSource.GITHUB else "Telegram"
        card = (
            f"{emoji} {task.title[:70]}\n"
            f"ID: {task.id}\n"
            f"Источник: {self._source_tag(task)} {src}\n"
            f"Статус: {self._task_status_label(task.status)}\n"
            f"Модель: {model}\n"
            f"Попытка: {task.attempt}\n"
            f"Стоимость: {self._cost_line(task)}\n"
            f"PR: {pr}\n"
            f"Создана: {created}"
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

    async def _restart_task(self, task: Task, query: dict) -> None:
        cqid = query.get("callback_query_id", "")
        chat_id_int = int(query.get("chat_id") or 0)
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
        self._spawn(self._process_task(task))

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
                comment_id = self.github.create_comment(num, f"❓ {question}")
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
                self.github.create_comment(int(task.source_id), text)
            except Exception as exc:
                logger.error("[clarify] github confirmation failed: %s", exc)

    async def _wait_for_clarification(self, task: Task) -> None:
        info = self._pending_clarifications.get(task.id)
        if not info:
            # Рестарт/сбой потерял состояние вопроса (живёт в памяти).
            # Переспрашиваем — иначе задача молча падала бы после каждого
            # перезапуска сервиса во время ожидания уточнений.
            logger.warning("[clarify] %s: pending state lost, re-asking", task.id)
            await self._ask_clarification(task)
            return

        asked_at: datetime = info["asked_at"]
        timeout = timedelta(seconds=self.config.clarification_timeout_seconds)
        if datetime.utcnow() - asked_at > timeout:
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

        if task.clarification_answer:
            answer = task.clarification_answer
            task.description += f"\n\nУточнение пользователя: {answer}"
            task.status = TaskStatus.READY
            self.state.save(task)
            self._pending_clarifications.pop(task.id, None)
            await self._confirm_clarification_accepted(task)
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
                await self._confirm_clarification_accepted(task)
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
            f"{secrets_note}"
            "Не создавай PR — это сделает оркестратор отдельно."
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
        text, options = self._parse_question_options(question)
        fut = asyncio.get_event_loop().create_future()
        self._pending_agent_questions[task.id] = {
            "task": task,
            "conversation_id": conversation_id,
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
                question_comment_id = self.github.create_comment(num, "\n".join(lines))
                qinfo["github_issue"] = num
                # Ответом считаем только комментарии ПОСЛЕ этого вопроса.
                qinfo["github_after_comment_id"] = question_comment_id
            except Exception as exc:  # noqa: BLE001
                logger.error("[agent-question] github comment failed: %s", exc)

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
                self._update_github_question_comment(
                    question_comment_id, text, "⏰ Время ожидания ответа истекло."
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
            self._update_github_question_comment(
                question_comment_id, text, f"✅ Ответ принят: {answer[:200]}"
            )
        try:
            await self.openhands.send_message(conversation_id, answer)
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

    def _update_github_question_comment(
        self, comment_id: int, question: str, footer: str
    ) -> None:
        """Дописать итог (ответ/истечение) в комментарий-вопрос issue."""
        try:
            self.github.update_comment(comment_id, f"❓ {question}\n\n{footer}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[agent-question] github comment update failed: %s", exc)

    async def _monitor_openhands(self, task: Task, conversation_id: str, client_dir: str) -> None:
        """Мониторит сессию OpenHands: уточнения, завершение, ошибки.

        Ориентируемся на состояние агента (AgentState), а не на статус
        разговора: разговор переходит в STOPPED и при успехе, и при ошибке,
        поэтому по нему результат не определить.
        """
        state_misses = 0
        # Build-секреты: повторяем установку, пока OpenHands не склонировал
        # репозиторий в /workspace (покрывает и старт, и recovery).
        secrets_done = not self._has_build_secrets()
        secrets_tries = 0
        while True:
            if task.status == TaskStatus.CANCELLED:
                task.add_log("Мониторинг остановлен: задача отменена")
                self.state.save(task)
                return
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

            # Агент ждёт ответа пользователя — либо вопрос, либо финальный отчёт.
            if state_val == "awaiting_user_input":
                message = (info.get("message") or "").strip()
                wants_response = bool(info.get("wait_for_response"))
                if wants_response or self._looks_like_clarification(message):
                    # Вопрос агента в процессе работы: показываем, ждём ответ,
                    # отправляем в тот же разговор и продолжаем мониторинг.
                    answered = await self._handle_agent_question(
                        task, conversation_id, message
                    )
                    if task.status == TaskStatus.CANCELLED:
                        return
                    if not answered:
                        task.status = TaskStatus.FAILED
                        task.add_log("Ответ на вопрос агента не получен (таймаут)")
                        await self._finalize_cost(task)
                        self.state.save(task)
                        self._notify_author(
                            task,
                            "⏰ Не дождался ответа на вопрос — задача остановлена.\n"
                            f"Вопрос был: {message[:200]}",
                        )
                        return
                    continue
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
                await self._finalize_cost(task)
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

    async def _build_ios_and_create_pr(self, task: Task, client_dir: str) -> None:
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
            task.add_log("Starting iOS build on iMac")
            ios_res = await self.ios_runner.build_ios(client_dir)
            task.add_log(f"iOS result: {ios_res.get('status')}")
            if ios_res.get("status") not in ("ok", "skipped"):
                error_msg = ios_res.get('error', 'неизвестная ошибка')
                task.status = TaskStatus.FAILED
                self.state.save(task)
                task.add_log(f"iOS failed: {error_msg}")
                log_text = "\n".join(task.log[-15:]) if task.log else ""
                self._notify_author(
                    task,
                    f"❌ iOS сборка не прошла.\n\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"Лог выполнения:\n{log_text}",
                )
                return
        else:
            task.add_log("iOS skipped: iMac not configured")

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
            f"✅ Задача выполнена! PR создан: {pr_url}\n"
            f"Стоимость: {self._cost_line(task)}",
        )

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #
    async def _recover_interrupted_tasks(self) -> None:
        """На старте: найти прерванные (сбой/перезагрузка) задачи и продолжить.

        Задача в RUNNING/READY/CLARIFYING остаётся «в воздухе», если сервис упал
        или машину перезагрузили во время работы. Уведомляем автора и админа о
        сбое, затем продолжаем: переподключаемся к живому разговору OpenHands
        либо перезапускаем задачу.
        """
        interrupted: list[Task] = []
        for status in (TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.CLARIFYING):
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
        """Продолжить прерванную задачу: переподключиться к разговору или перезапуск."""
        if task.status == TaskStatus.CLARIFYING:
            # Ждали ответа пользователя — снова встаём в ожидание (в _process_task
            # ответ, пришедший до сбоя, сам продолжит задачу).
            self._pending_clarifications[task.id] = {
                "task": task, "future": None, "asked_at": task.created_at,
            }
            self._spawn(self._process_task(task))
            return

        if task.status == TaskStatus.RUNNING and task.conversation_id:
            # Пробуем переподключиться к разговору OpenHands — мог уцелеть на диске.
            info = await self.openhands.get_agent_state(task.conversation_id)
            if info.get("agent_state") is not None:
                ws = self._ensure_workspace(task)
                client_dir = os.path.join(ws, "social_mind")
                task.add_log("Восстановление: переподключение к разговору OpenHands")
                self.state.save(task)
                asyncio.create_task(
                    self._monitor_openhands(task, task.conversation_id, client_dir)
                )
                return
            task.add_log("Восстановление: разговор OpenHands недоступен — перезапуск")
            self.state.save(task)

        # Разговор мёртв либо задача READY — перезапускаем с новой попыткой.
        if task.attempt >= self.max_resume_attempts:
            task.status = TaskStatus.FAILED
            task.add_log(f"Превышен лимит перезапусков ({task.attempt})")
            await self._finalize_cost(task)
            self.state.save(task)
            self._notify_author(
                task,
                f"❌ Задача перезапускалась после сбоев {task.attempt} раз и остановлена.",
            )
            self._notify_admin(f"❌ Задача {task.id} остановлена: превышен лимит перезапусков.")
            return
        task.attempt += 1
        task.status = TaskStatus.READY
        self.state.save(task)
        asyncio.create_task(self._run_openhands(task))

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

        # Запуск Telegram polling с retry при сетевых сбоях
        await self._start_telegram_with_retry()

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

        while True:
            try:
                await self.poll_github()
            except Exception as exc:
                logger.error("[loop] github poll error: %s", exc)

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
