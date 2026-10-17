"""Загрузка конфигурации из .env и config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class Config:
    def __init__(self, env_path: str = ".env", yaml_path: str = "orchestrator/config.yaml") -> None:
        base = Path(__file__).resolve().parents[2]  # корень проекта
        load_dotenv(base / env_path, override=False)

        with open(base / yaml_path, "r") as f:
            self._raw = yaml.safe_load(f)

    def _env(self, key: str, default: Any = None) -> Any:
        return os.getenv(key, default)

    def _raw_get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self._raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    # --- GitHub ---
    @property
    def github_app_id(self) -> int | None:
        v = self._env("GITHUB_APP_ID")
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    @property
    def github_private_key_path(self) -> str | None:
        return self._env("GITHUB_APP_PRIVATE_KEY_PATH")

    @property
    def github_installation_id(self) -> int | None:
        v = self._env("GITHUB_APP_INSTALLATION_ID")
        try:
            return int(v) if v else None
        except (ValueError, TypeError):
            return None

    @property
    def github_configured(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_private_key_path
            and self.github_installation_id
        )

    @property
    def github_repo(self) -> str:
        return self._env("GITHUB_REPO", self._raw_get("github", "repo", default=""))

    @property
    def github_label(self) -> str:
        return self._env("GITHUB_LABEL", self._raw_get("github", "label", default="aitask"))

    @property
    def github_poll_interval(self) -> int:
        return int(
            self._env("GITHUB_POLL_INTERVAL_SECONDS")
            or self._raw_get("github", "poll_interval_seconds", default=60)
        )

    # --- Telegram ---
    @property
    def telegram_bot_token(self) -> str:
        return self._env("TELEGRAM_BOT_TOKEN", self._raw_get("telegram", "bot_token", default=""))

    @property
    def telegram_admin_username(self) -> str:
        return self._env("TELEGRAM_ADMIN_USERNAME", self._raw_get("telegram", "admin_username", default=""))

    @property
    def telegram_allowed_usernames(self) -> list[str]:
        raw = self._env("TELEGRAM_ALLOWED_USERNAMES", "")
        if raw:
            return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]
        val = self._raw_get("telegram", "allowed_usernames", default=[])
        return list(val) if val else []

    # --- OpenHands ---
    @property
    def openhands_base_url(self) -> str:
        return self._env("OPENHANDS_BASE_URL", self._raw_get("openhands", "base_url", default="http://localhost:3000"))

    @property
    def openhands_api_key(self) -> str | None:
        return self._env("OPENHANDS_API_KEY")

    @property
    def openhands_llm_base_url(self) -> str:
        return self._env(
            "OPENHANDS_LLM_BASE_URL",
            self._raw_get("openhands", "llm_base_url", default="https://api.provod.ai/v1"),
        )

    @property
    def openhands_llm_api_key(self) -> str:
        return self._env(
            "OPENHANDS_LLM_API_KEY",
            self._raw_get("openhands", "llm_api_key", default=""),
        )

    @property
    def openhands_llm_model(self) -> str:
        return self._env(
            "OPENHANDS_LLM_MODEL",
            self._raw_get("openhands", "llm_model", default="gpt-4o"),
        )

    # --- iMac ---
    @property
    def imac_host(self) -> str | None:
        return self._env("IMAC_HOST", self._raw_get("imac", "host"))

    @property
    def imac_user(self) -> str | None:
        return self._env("IMAC_USER", self._raw_get("imac", "user"))

    @property
    def imac_ssh_key(self) -> str:
        return self._env("IMAC_SSH_KEY", self._raw_get("imac", "ssh_key", default="~/.ssh/id_ed25519"))

    @property
    def imac_work_dir(self) -> str:
        return self._env("IMAC_WORK_DIR", self._raw_get("imac", "work_dir", default="~/personal_worker"))

    # --- Paths ---
    @property
    def work_dir(self) -> str:
        return self._env("WORK_DIR", self._raw_get("paths", "work_dir", default="/mnt/ssd/personal_worker/workspaces"))

    @property
    def state_db(self) -> str:
        return self._env("STATE_DB", self._raw_get("paths", "state_db", default="/mnt/ssd/personal_worker/state/orchestrator.sqlite"))

    # --- Flutter ---
    @property
    def flutter_image(self) -> str:
        return self._env("FLUTTER_IMAGE", self._raw_get("flutter", "image", default="cirrusci/flutter:stable"))

    @property
    def flutter_project_subpath(self) -> str:
        return self._env("FLUTTER_PROJECT_SUBPATH", self._raw_get("flutter", "project_subpath", default="domains/client"))

    # --- Clarification ---
    @property
    def clarification_timeout_seconds(self) -> int:
        return int(
            self._env("CLARIFICATION_TIMEOUT_SECONDS")
            or self._raw_get("scheduler", "clarification_timeout_seconds", default=10800)
        )

    @property
    def clarification_poll_seconds(self) -> int:
        return int(
            self._env("CLARIFICATION_POLL_SECONDS")
            or self._raw_get("scheduler", "clarification_poll_seconds", default=30)
        )

    # --- Scheduler ---
    @property
    def poll_interval_seconds(self) -> int:
        return int(
            self._env("SCHEDULER_POLL_INTERVAL")
            or self._raw_get("scheduler", "poll_interval_seconds", default=60)
        )
