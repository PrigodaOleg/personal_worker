"""OpenHands Adapter: правильный API OpenHands 0.35.

Поток:
1. POST /api/conversations — создать разговор с initial_user_msg
2. POST /api/conversations/{id}/start — запустить агента
3. GET /api/conversations/{id} — статус
4. GET /api/conversations/{id}/trajectory — события/логи
5. POST /api/settings — настроить LLM (provod.ai)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class OpenHandsAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def _headers(self) -> dict:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def configure_llm(self) -> dict:
        """Настроить LLM провайдера (provod.ai) через /api/settings."""
        payload = {
            "llm_model": self.llm_model,
            "llm_api_key": self.llm_api_key,
            "llm_base_url": self.llm_base_url,
        }
        try:
            r = await self._client.post(
                f"{self.base_url}/api/settings",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] configure_llm failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def create_conversation(
        self,
        instruction: str,
        repository: Optional[str] = None,
        branch: str = "main",
    ) -> dict:
        """Создать разговор с инструкцией для агента."""
        payload: dict = {
            "initial_user_msg": instruction,
        }
        if repository:
            payload["repository"] = repository
            payload["selected_branch"] = branch
        try:
            r = await self._client.post(
                f"{self.base_url}/api/conversations",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] create_conversation failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def start_conversation(self, conversation_id: str) -> dict:
        """Запустить agente loop для разговора."""
        payload: dict = {}
        try:
            r = await self._client.post(
                f"{self.base_url}/api/conversations/{conversation_id}/start",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] start_conversation failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def get_status(self, conversation_id: str) -> dict:
        """Получить статус разговора."""
        try:
            r = await self._client.get(
                f"{self.base_url}/api/conversations/{conversation_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] get_status failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def get_trajectory(self, conversation_id: str) -> dict:
        """Получить траекторию событий (логи выполнения агента)."""
        try:
            r = await self._client.get(
                f"{self.base_url}/api/conversations/{conversation_id}/trajectory",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] get_trajectory failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def send_message(self, conversation_id: str, message: str) -> dict:
        """Отправить сообщение в разговор (ответ на уточнение)."""
        payload = {"message": message}
        try:
            r = await self._client.post(
                f"{self.base_url}/api/conversations/{conversation_id}/message",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] send_message failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
