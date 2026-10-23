#!/usr/bin/env python3
"""Залить секреты из локального .env в GitHub Secrets репозитория.

Запускается ОДИН РАЗ (и при ротации секретов). После этого деплой
(.github/workflows/deploy.yml, шаг «Render secrets») сам собирает .env
и PEM-ключ на целевой машине — вручную ничего копировать не нужно.

Секреты:
  TELEGRAM_BOT_TOKEN      — из .env
  OPENHANDS_API_KEY       — из .env
  OPENHANDS_LLM_API_KEY   — из .env
  GITHUB_APP_PRIVATE_KEY  — содержимое PEM-файла из .env (GITHUB_APP_PRIVATE_KEY_PATH)

Аутентификация (в порядке приоритета):
  1. GH_TOKEN  — Personal Access Token (classic: repo; fine-grained: Secrets rw)
  2. GitHub App из orchestrator/config.yaml — ему нужно разрешение
     «Secrets: Read and write» (Settings → Developer settings → GitHub Apps).

Значения шифруются libsodium sealed box публичным ключом репозитория —
в открытом виде по сети не ходят (требование GitHub API).

Использование:
  GH_TOKEN=... python3 deploy/sync_secrets.py
  python3 deploy/sync_secrets.py            # через App (после выдачи прав)
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPO = "PrigodaOleg/personal_worker"
API = f"https://api.github.com/repos/{REPO}/actions/secrets"

# Имя секрета → источник значения (имя переменной .env / PEM-файла).
ENV_SECRET_SOURCES = {
    "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "OPENHANDS_API_KEY": "OPENHANDS_API_KEY",
    "OPENHANDS_LLM_API_KEY": "OPENHANDS_LLM_API_KEY",
}


def load_dotenv_values(path: Path) -> dict[str, str]:
    """Простой парсер .env: KEY=VALUE, комментарии # пропускаются."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Значения с инлайновым комментарием (# ...) — обрезаем.
        val = val.split(" #")[0].strip().strip('"').strip("'")
        values[key.strip()] = val
    return values


def gh_token() -> str:
    if os.getenv("GH_TOKEN"):
        return os.getenv("GH_TOKEN", "")
    # Фолбэк: installation token GitHub App (нужно право Secrets: write).
    sys.path.insert(0, str(PROJECT))
    from orchestrator.core.config import Config
    from orchestrator.adapters.github import GitHubAdapter

    config = Config()
    gh = GitHubAdapter(
        app_id=config.github_app_id or 0,
        private_key_path=config.github_private_key_path or "",
        installation_id=config.github_installation_id or 0,
        repo_full_name=REPO,
        label="aitask",
    )
    gh._ensure_auth()
    return gh._repo.requester.auth.token  # type: ignore[union-attr]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT / ".env"))
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"❌ {env_path} не найден")
        return 1
    dotenv = load_dotenv_values(env_path)

    import requests
    from nacl import encoding, public

    token = gh_token()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    # 1. Публичный ключ репозитория для шифрования.
    r = requests.get(API, headers=headers, timeout=30)
    if r.status_code == 403:
        print("❌ 403: нет права на секреты. Выдайте App разрешение «Secrets: "
              "Read and write» (Settings → Developer settings → GitHub Apps → "
              "Permissions) или передайте GH_TOKEN (PAT с repo scope).")
        return 1
    r.raise_for_status()
    pk_data = r.json()
    pk = public.PublicKey(pk_data["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk)

    # 2. Собираем значения.
    to_sync: dict[str, str] = {}
    missing: list[str] = []
    for secret_name, env_name in ENV_SECRET_SOURCES.items():
        val = dotenv.get(env_name, "")
        if val:
            to_sync[secret_name] = val
        else:
            missing.append(f"{secret_name} (нет {env_name} в .env)")

    pem_path = dotenv.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if pem_path and Path(pem_path).expanduser().exists():
        to_sync["GITHUB_APP_PRIVATE_KEY"] = (
            Path(pem_path).expanduser().read_text()
        )
    else:
        missing.append("GITHUB_APP_PRIVATE_KEY (GITHUB_APP_PRIVATE_KEY_PATH "
                       "не задан или файл не существует)")

    if missing:
        print("⚠️  Пропущены (источник пуст):")
        for m in missing:
            print(f"   - {m}")

    # 3. Шифруем и льём.
    failed = 0
    for name, value in to_sync.items():
        enc = sealed.encrypt(value.encode())
        payload = {
            "encrypted_value": base64.b64encode(enc).decode(),
            "key_id": pk_data["key_id"],
        }
        r = requests.put(f"{API}/{name}", headers=headers, json=payload, timeout=30)
        status = "✅" if r.status_code in (201, 204) else f"❌ {r.status_code}"
        print(f"{status} {name}")
        if r.status_code not in (201, 204):
            print(f"   {r.text[:200]}")
            failed += 1

    print(f"\nГотово: {len(to_sync) - failed}/{len(to_sync)} секретов в {REPO}.")
    if to_sync:
        print("Дальше деплой соберёт .env сам (шаг «Render secrets»).")
    return 1 if failed else (2 if missing else 0)


if __name__ == "__main__":
    raise SystemExit(main())
