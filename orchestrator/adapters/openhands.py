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
import subprocess
import time
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)


class OpenHandsAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_model: Optional[str] = None,
        github_app_id: Optional[int] = None,
        github_private_key_path: Optional[str] = None,
        github_installation_id: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self.github_app_id = github_app_id
        self.github_private_key_path = github_private_key_path
        self.github_installation_id = github_installation_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._providers_configured: bool = False

    def _headers(self) -> dict:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get_github_installation_token(self) -> Optional[str]:
        """Получить свежий installation token от GitHub App."""
        if not all([self.github_app_id, self.github_private_key_path, self.github_installation_id]):
            return None
        try:
            private_key = open(self.github_private_key_path).read()
            now = int(time.time())
            payload = {
                "iat": now - 60,
                "exp": now + 600,
                "iss": self.github_app_id,
            }
            jwt_token = jwt.encode(payload, private_key, algorithm="RS256")
            result = subprocess.run(
                [
                    "curl", "-s", "-m", "10", "-X", "POST",
                    f"https://api.github.com/app/installations/{self.github_installation_id}/access_tokens",
                    "-H", f"Authorization: Bearer {jwt_token}",
                    "-H", "Accept: application/vnd.github+json",
                ],
                capture_output=True, text=True, timeout=15,
            )
            import json
            resp = json.loads(result.stdout)
            return resp.get("token")
        except Exception as exc:
            logger.error("[openhands] failed to get installation token: %s", exc)
            return None

    async def configure_github_provider(self) -> dict:
        """Настроить GitHub provider tokens в OpenHands."""
        token = self._get_github_installation_token()
        if not token:
            return {"status": "error", "error": "no GitHub installation token"}

        payload = {
            "provider_tokens": {
                "github": {
                    "token": token,
                    "user_id": str(self.github_installation_id),
                    "host": "github.com",
                }
            }
        }
        try:
            r = await self._client.post(
                f"{self.base_url}/api/add-git-providers",
                json=payload,
                headers=self._headers(),
            )
            r.raise_for_status()
            self._providers_configured = True
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] configure_github_provider failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def configure_llm(self, model: Optional[str] = None) -> dict:
        """Настроить LLM провайдера (provod.ai) через /api/settings.

        `model` — переопределение модели для конкретного разговора. Если None,
        берётся модель по умолчанию из конфига. Модель нормализуется к
        префиксу `openai/` — он говорит LiteLLM, что эндпоинт OpenAI-совместим.
        """
        resolved = model or self.llm_model
        if resolved and not resolved.startswith("openai/"):
            resolved = f"openai/{resolved}"
        payload = {
            "llm_model": resolved,
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

    async def list_models(self) -> dict:
        """Список доступных моделей у LLM-провайдера (provod.ai /v1/models).

        Возвращает id моделей без префикса провайдера (например,
        `claude-sonnet-4.6`) — их удобно показывать в клавиатуре.
        """
        raw = await self.list_models_raw()
        if raw.get("status") != "ok":
            return raw
        models = sorted({m["id"].split("/", 1)[-1] for m in raw["models"] if m.get("id")})
        return {"status": "ok", "models": models}

    async def list_models_raw(self) -> dict:
        """Полный список моделей с метаданными (цены, контекст и т.д.)."""
        if not self.llm_base_url or not self.llm_api_key:
            return {"status": "error", "error": "LLM не настроен"}
        url = self.llm_base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {self.llm_api_key}"}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            models = [m for m in items if isinstance(m, dict) and m.get("id")]
            return {"status": "ok", "models": models}
        except httpx.HTTPError as exc:
            logger.error("[openhands] list_models_raw failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    async def get_usage(self, conversation_id: str) -> dict:
        """Использование токенов разговором (из llm_metrics событий).

        Возвращает накопленные токены: prompt/completion/cache_read. Источник —
        `GET /api/conversations/{id}/events`, у событий есть `llm_metrics` с
        `accumulated_token_usage`.
        """
        empty = {"prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0}
        try:
            r = await self._client.get(
                f"{self.base_url}/api/conversations/{conversation_id}/events",
                headers=self._headers(),
            )
            r.raise_for_status()
            events = r.json().get("events", [])
        except httpx.HTTPError as exc:
            logger.error("[openhands] get_usage failed: %s", exc)
            return empty

        acc = empty
        for e in events:
            metrics = e.get("llm_metrics") or {}
            usage = metrics.get("accumulated_token_usage") or {}
            if usage:
                acc = {
                    "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                    "completion_tokens": usage.get("completion_tokens", 0) or 0,
                    "cache_read_tokens": usage.get("cache_read_tokens", 0) or 0,
                }
        return acc

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
            logger.info("[openhands] start_conversation status=%s body=%s", r.status_code, r.text[:500])
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            logger.error("[openhands] start_conversation failed: status=%s body=%s", 
                        getattr(exc, 'response', None) and getattr(exc.response, 'status_code', '?'),
                        getattr(exc, 'response', None) and getattr(exc.response, 'text', 'no body')[:500])
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

    async def get_agent_state(self, conversation_id: str) -> dict:
        """Состояние агента и его последнее сообщение.

        Статус разговора (get_status) показывает жизнь runtime-контейнера,
        а не результат работы агента: после успешного завершения разговор
        переходит в STOPPED. Реальное состояние агента видно только в
        траектории — в событиях agent_state_changed (см. AgentState).
        """
        traj = await self.get_trajectory(conversation_id)
        if traj.get("status") == "error":
            return {
                "agent_state": None,
                "message": None,
                "completed": False,
                "error": traj.get("error"),
            }

        events = traj.get("trajectory") or traj.get("events") or traj
        if not isinstance(events, list):
            return {
                "agent_state": None,
                "message": None,
                "completed": False,
                "error": "нет событий в траектории",
            }

        agent_state = None
        message = None
        completed = False
        wait_for_response = False
        for event in events:
            if event.get("observation") == "agent_state_changed":
                extras = event.get("extras") or {}
                if extras.get("agent_state"):
                    agent_state = extras["agent_state"]
                    if agent_state in ("awaiting_user_input", "finished"):
                        # Агент отчитался — дальше разговор просто простаивает,
                        # пока OpenHands его не остановит.
                        completed = True
            if event.get("source") == "agent" and event.get("action") == "message":
                message = event.get("message")
                args = event.get("args") or {}
                wait_for_response = bool(args.get("wait_for_response"))

        return {
            "agent_state": agent_state,
            "message": message,
            "wait_for_response": wait_for_response,
            "completed": completed,
            "error": None,
        }

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
