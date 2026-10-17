# personal_worker — Оркестратор задач разработки Flutter

Система принимает задачи из **GitHub Issues** или **Telegram**, запускает их через
**OpenHands**, разрабатывает код на **Flutter**, запускает тесты и создаёт **PR** в GitHub.
При необходимости уточнений — спрашивает мнение пользователя.

Сборка и тесты для **Linux / Web / Android** — внутри контейнера OpenHands (на этом компьютере).
Сборка и тесты для **iOS** — на iMac в той же подсети (по SSH).

## Стек

- Оркестратор: Python 3.14+
- Агент кода: OpenHands (Docker, со встроенным Flutter SDK)
- LLM: провайдер provod.ai (`https://api.provod.ai/v1`)
- Хранение состояния: SQLite (один файл, без внешней БД)

## Структура

```
personal_worker/
├── README.md
├── .env
├── .gitignore
├── docker-compose.yml
├── requirements.txt
├── Makefile
├── docker/
│   └── opentands-flutter/Dockerfile   # OpenHands + Flutter SDK
├── orchestrator/
│   ├── main.py              # точка входа
│   ├── core/
│   │   ├── config.py        # загрузка .env + config.yaml
│   │   ├── task.py          # модель задачи (статус, источник, payload)
│   │   ├── scheduler.py     # очередь задач, уточнения, мониторинг OpenHands, iOS, PR
│   │   └── state.py         # SQLite-хранилище
│   ├── adapters/
│   │   ├── github.py        # GitHub App (JWT), issues, комментарии, PR
│   │   ├── telegram.py      # Telegram Bot (polling)
│   │   ├── openhands.py     # OpenHands API (запуск, статус, ответы на уточнения)
│   │   └── imac.py          # iOS сборка/тесты на iMac по SSH
│   └── runners/
│       └── ios.py           # делегирование iOS на iMac
├── deploy/
│   ├── personal_worker.service   # systemd: 24/7, авторестарт, автозапуск при загрузке ОС
│   ├── install.sh                # установка сервиса одной командой
│   └── GITHUB_APP_SETUP.md       # создание GitHub App
├── tests/
└── docs/
    └── ARCHITECTURE.md
```

## Быстрый старт

```bash
# 1. Перейдите в папку проекта
cd /mnt/ssd/personal_worker

# 2. Заполните .env (реальные значения уже там, кроме TODO-полей)
nano .env

# 3. Установите зависимости Python
make install

# 4. Соберите и запустите OpenHands с Flutter
make up

# 5. Запустите оркестратор
make run
```

## 24/7 режим (systemd)

```bash
sudo ./deploy/install.sh
```

Сервис поднимет Docker, запустит оркестратор и будет перезапускаться при сбоях
и при загрузке ОС. Состояние восстанавливается из SQLite.

## ❗ Что нужно сделать перед первым запуском

| Что | Где |
|-----|-----|
| GitHub App ID и Installation ID | `.env`, `orchestrator/config.yaml` |
| Приватный ключ GitHub App | `config/social_mind_app_private_key.pem` |
| Инструкция по созданию GitHub App | `deploy/GITHUB_APP_SETUP.md` |
| iMac host/user (для iOS) | `.env` (можно добавить позже) |
| SSH-ключ на iMac | `~/.ssh/id_ed25519.pub` → `~/.ssh/authorized_keys` на iMac |
