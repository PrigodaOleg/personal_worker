"""Telegram Adapter: приём команд, уточнения, уведомления.

Использует python-telegram-bot в режиме polling (ApplicationBuilder).
Отвечает тому, кто прислал задачу.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


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

    def _is_allowed(self, username: Optional[str]) -> bool:
        if not self.allowed_usernames:
            return True
        return bool(username and username in self.allowed_usernames)

    def set_message_callback(self, callback: callable) -> None:
        """callback(update_dict) — вызывается на входящее сообщение."""
        self._message_callback = callback

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
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handler)
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

    async def send_message(self, chat_id: int, text: str) -> None:
        if self._app:
            await self._app.bot.send_message(chat_id=chat_id, text=text)

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
