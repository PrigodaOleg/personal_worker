"""OpenHands Adapter: запуск агента, мониторинг, обнаружение уточнений.

Взаимодействует с OpenHands REST API (порт 3000).
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

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/api/health")
            return r.status_code == 200
        except Exception:
            return False

    async def start_task(
        self,
        instruction: str,
        work_dir: str,
        repo_url: str,
        branch: str,
    ) -> dict:
        """Запустить OpenHands с инструкцией. Вернуть информацию о сессии."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "instruction": instruction,
            "work_dir": work_dir,
            "repo_url": repo_url,
            "branch": branch,
            "llm": {
                "base_url": self.llm_base_url,
                "api_key": self.llm_api_key,
                "model": self.llm_model,
            },
        }
        try:
            r = await self._client.post(
                f"{self.base_url}/api/tasks", json=payload, headers=headers
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] start_task failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def get_status(self, session_id: str) -> dict:
        """Получить статус сессии OpenHands."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            r = await self._client.get(
                f"{self.base_url}/api/tasks/{session_id}", headers=headers
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] get_status failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def send_answer(self, session_id: str, answer: str) -> dict:
        """Отправить ответ на уточнение агенту."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            r = await self._client.post(
                f"{self.base_url}/api/tasks/{session_id}/answer",
                json={"answer": answer},
                headers=headers,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] send_answer failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
