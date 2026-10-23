#!/usr/bin/env python3
"""Собрать .env и PEM-ключ GitHub App из GitHub Secrets (вызывается деплоем).

Разворачивание с нуля: репозиторий + GitHub Secrets → рабочая установка.
Не-секретная конфигурация живёт в orchestrator/config.yaml (в git),
секреты приходят через окружение шага «Render secrets» в deploy.yml
(значения ${{ secrets.* }} Telegram/Actions маскирует в логах автоматически).

Пишет:
  <project>/.env                              — только секреты + путь к ключу
  <project>/config/github-app.private-key.pem — ключ App (0600)

Падает с внятным списком, если какой-то секрет не задан.
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Секрет → имя переменной в .env. Все обязаны присутствовать.
SECRET_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "OPENHANDS_API_KEY",
    "OPENHANDS_LLM_API_KEY",
]

# Путь к ключу фиксирован, чтобы config.yaml в git указывал на него.
KEY_REL_PATH = "config/github-app.private-key.pem"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()
    project = args.project.resolve()

    missing = [v for v in SECRET_ENV_VARS if not os_env(v)]
    pem = os_env("GH_APP_PRIVATE_KEY", "")
    if not pem:
        missing.append("GH_APP_PRIVATE_KEY")
    if missing:
        print("❌ Не заданы секреты репозитория (GitHub → Settings → Secrets "
              "and variables → Actions → Repository secrets):")
        for m in missing:
            print(f"   - {m}")
        print("Залить их: python3 deploy/sync_secrets.py (на машине с .env).")
        return 1

    # 1. PEM-ключ App.
    key_path = project / KEY_REL_PATH
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(pem if pem.endswith("\n") else pem + "\n")
    key_path.chmod(0o600)

    # 2. .env: только секреты и путь к ключу. Всё остальное — config.yaml.
    lines = [
        "# Сгенерирован деплоем из GitHub Secrets (deploy/render_secrets.py).",
        "# НЕ редактируйте вручную — перезапишется следующим деплоем.",
        "# Не-секретные настройки: orchestrator/config.yaml.",
        *(
            f"{var}={os_env(var)}"
            for var in SECRET_ENV_VARS
        ),
        f"GITHUB_APP_PRIVATE_KEY_PATH={key_path}",
        "",
    ]
    env_path = project / ".env"
    env_path.write_text("\n".join(lines))
    env_path.chmod(0o600)

    print(f"✅ .env записан ({env_path})")
    print(f"✅ ключ App записан ({key_path})")
    return 0


def os_env(name: str, default: str = "") -> str:
    import os
    return os.getenv(name, default)


if __name__ == "__main__":
    raise SystemExit(main())
