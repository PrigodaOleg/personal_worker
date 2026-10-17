"""Telegram adapter: приём команд, уточнения, уведомления (заглушка)."""

from __future__ import annotations


class TelegramAdapter:
    def __init__(self, bot_token: str, admin_chat_id: int) -> None:
        self.bot_token = bot_token
        self.admin_chat_id = admin_chat_id

    async def send_message(self, chat_id: int, text: str) -> None:
        """Отправить сообщение в Telegram."""
        print(f"[telegram stub] send to {chat_id}: {text}")

    async def ask_clarification(self, chat_id: int, question: str) -> str:
        """Задать уточняющий вопрос и дождаться ответа."""
        print(f"[telegram stub] ask {chat_id}: {question}")
        return "TODO_WAIT_FOR_REAL_IMPLEMENTATION"
