# personal_worker — Оркестратор задач разработки Flutter

Система принимает задачи из **GitHub Issues** или **Telegram**, запускает их через
**OpenHands**, разрабатывает код на **Flutter**, запускает тесты и создаёт **PR** в GitHub.
При необходимости уточнений — спрашивает мнение пользователя.

Сборка и тесты для **Linux / Web / Android** — на этом компьютере.
Сборка и тесты для **iOS** — на iMac в той же подсети (по SSH).

## Стек

- Оркестратор: Python 3.14+
- Агент кода: OpenHands (Docker)
- Сборка Flutter: Docker-образы с Flutter SDK (локально) + iMac по SSH (iOS)
- Хранение состояния: SQLite (один файл, без внешней БД)

## Структура

```
personal_worker/
├── README.md
├── .gitignore
├── .env.example
├── docker-compose.yml
├── requirements.txt
├── Makefile
├── orchestrator/
│   ├── __init__.py
│   ├── main.py              # точка входа (CLI / polling / webhook)
│   ├── config.yaml.example  # пример конфига
│   ├── core/
│   │   ├── __init__.py
│   │   ├── task.py          # модель задачи (статус, источник, payload)
│   │   ├── scheduler.py     # очередь задач
│   │   └── state.py         # SQLite-хранилище
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── github.py        # GitHub API (issues, комментарии, PR)
│   │   ├── telegram.py      # Telegram Bot API
│   │   ├── openhands.py     # OpenHands runtime (Docker / API)
│   │   └── imac.py          # iOS сборка/тесты на iMac по SSH
│   └── runners/
│       ├── __init__.py
│       ├── flutter_base.py  # общая логика flutter build/test
│       ├── linux.py
│       ├── web.py
│       ├── android.py
│       └── ios.py           # делегирование на iMac
├── tests/
│   └── __init__.py
└── docs/
    └── ARCHITECTURE.md
```

## Быстрый старт

```bash
# 1. Перейдите в папку проекта
cd /mnt/ssd/personal_worker

# 2. Скопируйте пример окружения и заполните реальными значениями
cp .env.example .env
nano .env

# 3. Установите зависимости Python
make install

# 4. Поднимите OpenHands (см. docs/ARCHITECTURE.md)
make up

# 5. Запустите оркестратор
make run
```

## ❗ ОБЯЗАТЕЛЬНО: ответьте на уточняющие вопросы

В `.env.example` и `orchestrator/config.yaml.example` помечены значения `TODO`.
Перед первым запуском нужно ответить на вопросы в разделе **[Вопросы к пользователю]**
ниже в этом README и подставить полученные значения в конфиг. После этого я
доношу реализацию адаптеров до рабочего состояния.

## Лицензия

MIT

