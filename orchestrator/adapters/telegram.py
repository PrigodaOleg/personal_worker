"""Telegram Adapter: приём команд, уточнения, уведомления.

Использует python-telegram-bot в режиме polling (ApplicationBuilder).
Отвечает тому, кто прислал задачу.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Union

import telegram
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# Кнопки постоянного меню (ReplyKeyboardMarkup). Текст кнопки = входящее сообщение,
# поэтому он же используется для маршрутизации к команде.
MENU_COMMANDS: dict[str, str] = {
    "📊 Статус": "/status",
    "📋 Задачи": "/tasks",
    "🧠 Модель": "/model",
    "🚫 Отмена": "/cancel",
    "❓ Помощь": "/help",
}


class TelegramAdapter:
    def __init__(
        self,
        bot_token: str,
        admin_username: str,
        allowed_usernames: list[str],
    ) -> None:
        self.bot_token = bot_token
        self.admin_username = admin_username.lstrip("@")
        self.allowed_usernames = {u.lstrip("@") for u in allowed_usernames if u}
        self._app: Optional[Application] = None
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._message_callback: Optional[callable] = None
        self._callback_query_callback: Optional[callable] = None

    def _is_allowed(self, username: Optional[str]) -> bool:
        if not self.allowed_usernames:
            return True
        return bool(username and username in self.allowed_usernames)

    def set_message_callback(self, callback: callable) -> None:
        """callback(update_dict) — вызывается на входящее сообщение."""
        self._message_callback = callback

    def set_callback_query_callback(self, callback: callable) -> None:
        """callback(query_dict) — вызывается на нажатие инлайн-кнопки."""
        self._callback_query_callback = callback

    async def _callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cq = update.callback_query
        if not cq or not cq.from_user:
            return
        username = cq.from_user.username or ""
        if not self._is_allowed(username):
            await cq.answer("Нет доступа.")
            return
        # Отвечаем на callback сразу, иначе Telegram будет показывать "часики".
        try:
            await cq.answer()
        except Exception:  # noqa: BLE001
            pass
        if self._callback_query_callback:
            await self._callback_query_callback({
                "callback_query_id": cq.id,
                "chat_id": cq.message.chat.id if cq.message else None,
                "message_id": cq.message.message_id if cq.message else None,
                "user_id": cq.from_user.id,
                "username": username,
                "data": cq.data or "",
            })

    async def _handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.effective_message:
            return
        username = update.effective_user.username or ""
        if not self._is_allowed(username):
            await update.effective_message.reply_text(
                "Извините, у вас нет доступа к этому боту."
            )
            return
        msg = {
            "chat_id": update.effective_chat.id,
            "user_id": update.effective_user.id,
            "username": username,
            "text": update.effective_message.text or "",
            "message_id": update.effective_message.message_id,
        }
        if self._message_callback:
            await self._message_callback(msg)

    async def start_polling(self) -> None:
        self._app = (
            ApplicationBuilder()
            .token(self.bot_token)
            .build()
        )
        self._app.add_handler(
            MessageHandler(filters.TEXT, self._handler)
        )
        self._app.add_handler(
            CallbackQueryHandler(self._callback_handler)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        logger.info("[telegram] polling started")

    async def stop_polling(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def connect(self) -> None:
        """Поднять бота только на отправку (без поллинга).

        Нужно для одноразовых прогонов и скриптов: уведомления уходят,
        но входящие сообщения не обрабатываются.
        """
        if self._app:
            return
        self._app = ApplicationBuilder().token(self.bot_token).build()
        await self._app.initialize()

    async def close(self) -> None:
        if self._app:
            await self._app.shutdown()
            self._app = None

    @staticmethod
    def build_keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
        """Собрать инлайн-клавиатуру из списка [[(текст, callback_data), ...], ...]."""
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text, callback_data=data) for text, data in row]
             for row in rows]
        )

    @staticmethod
    def build_menu_keyboard() -> ReplyKeyboardMarkup:
        """Постоянное меню-клавиатура внизу чата (кнопки шлют текст)."""
        keys = list(MENU_COMMANDS.keys())
        rows = [keys[i:i + 2] for i in range(0, len(keys), 2)]
        return ReplyKeyboardMarkup(
            [[telegram.KeyboardButton(t) for t in row] for row in rows],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: Optional[Union[InlineKeyboardMarkup, ReplyKeyboardMarkup]] = None,
    ) -> Optional[int]:
        """Отправить сообщение, повторяя при сетевых сбоях.

        Telegram периодически отваливается по таймауту (особенно через VPN),
        и без повторов уведомление о результате задачи просто теряется.
        Возвращает message_id — его можно передать в edit_message.
        """
        if not self._app:
            return None

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                msg = await self._app.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=keyboard
                )
                return msg.message_id
            except (telegram.error.TimedOut, telegram.error.NetworkError) as exc:
                last_exc = exc
                logger.warning(
                    "[telegram] send failed (attempt %d/3): %s", attempt, exc
                )
                await asyncio.sleep(2 * attempt)
        logger.error("[telegram] message not delivered after 3 attempts: %s", last_exc)
        return None

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
    ) -> bool:
        """Обновить ранее отправленное сообщение (для навигации по кнопкам)."""
        if not self._app:
            return False
        try:
            await self._app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            return True
        except telegram.error.BadRequest as exc:
            # "Message is not modified" — не ошибка при повторном тапе.
            if "not modified" not in str(exc).lower():
                logger.warning("[telegram] edit_message failed: %s", exc)
            return False
        except (telegram.error.TimedOut, telegram.error.NetworkError) as exc:
            logger.warning("[telegram] edit_message network error: %s", exc)
            return False

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        if not self._app:
            return
        try:
            await self._app.bot.answer_callback_query(
                callback_query_id=callback_query_id, text=text
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[telegram] answer_callback_query failed: %s", exc)

    async def ask_clarification(self, chat_id: int, question: str) -> str:
        """Задать уточняющий вопрос и вернуть будущий ответ."""
        await self.send_message(chat_id, f"❓ Уточнение:\n{question}")
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[str]" = loop.create_future()
        key = str(chat_id)
        self._pending_responses[key] = future
        return await future

    def resolve_answer(self, chat_id: int, answer: str) -> None:
        """Вызвать из внешнего цикла, когда пришёл ответ на уточнение."""
        key = str(chat_id)
        if key in self._pending_responses:
            fut = self._pending_responses.pop(key)
            if not fut.done():
                fut.set_result(answer)
