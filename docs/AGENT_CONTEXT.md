# Контекст для следующей сессии с агентом

Документ для AI-агента, который продолжит работу над проектом. Здесь то, что
было выяснено опытным путём и не видно из кода: причины неочевидных решений,
грабли и текущее состояние. Обновляйте по мере работы.

## Что это за система

Оркестратор берёт задачу (GitHub Issue с меткой `aitask` или сообщение
Telegram), запускает её через OpenHands, собирает iOS на iMac по SSH, создаёт PR
в `PrigodaOleg/social_mind` и уведомляет автора. Поток задачи и разделение по
платформам — в [ARCHITECTURE.md](ARCHITECTURE.md).

Три слоя, которые важно не путать:

| Слой | Где | Что делает |
|------|-----|------------|
| Оркестратор | процесс на хосте, `python -m orchestrator.main` | очередь задач, Telegram, GitHub, PR |
| Сервер OpenHands | контейнер `openhands-flutter` | управляет агентом, поднимает runtime |
| Runtime | контейнер `openhands-runtime-<conversation_id>` | **здесь агент выполняет команды**, здесь живёт Flutter |

## Текущее состояние

Работает сквозной прогон: задача → агент в runtime-контейнере → `flutter
pub get` / `test` / `build linux` / `build web` / `build apk` (все зелёные;
APK собирается долго — Gradle гоняет Kotlin-компилятор по 9 минут).

Шаг PR прогнан в dry-run (`deploy/e2e_run.py`, см. ниже): синхронизация правок
из контейнера → `git commit` → `git push --dry-run` — всё зелёное. Реальный PR
в `social_mind` пока не создавался.

Не проверено на реальном прогоне:

- создание настоящего PR в `social_mind` и уведомление в Telegram (dry-run
  выключен — задайте `PR_DRY_RUN=0`/уберите переменную, чтобы PR создавались);
- путь iOS — `deploy/setup_imac.sh` ещё не запускался на iMac `192.168.88.20`,
  в `.env` стоят заглушки `TODO_IMAC_USER`, поэтому iOS-шаг честно пропускается.

### Как прогнать пайплайн без создания PR

```bash
PR_DRY_RUN=1 .venv/bin/python deploy/e2e_run.py
```

Скрипт прогоняет задачу целиком (агент в runtime → sync → git), но пуш делает
`--dry-run` и PR не создаёт. Умеет отправлять уведомление в Telegram (бот
поднимается на отправку без поллинга).

### Как правки агента попадают в PR

Агент работает в `/workspace/social_mind` внутри runtime-контейнера, а шаг PR
смотрит рабочую копию на хосте `workspaces/<task_id>/social_mind`. Контейнер
ничего не монтирует, поэтому `_sync_changes_from_runtime` вручную переносит
файлы: список берётся как `git diff --name-only origin/main` (правки агента
могут быть уже закоммичены им внутри контейнера — `git status` их не покажет!)
плюс `git ls-files --others`. При копировании сохраняются переводы строк
исходного файла (`_match_line_endings`): агент часто переписывает CRLF в LF, и
без этого весь файл попадает в diff.

## Грабли, на которые уже наступали

**VPN перебивает доступ к LLM.** OpenVPN при каждом подключении добавляет
маршруты `0.0.0.0/1` и `128.0.0.0/1`, и трафик к `api.provod.ai`
(`158.160.9.95`) уходит в туннель. Симптом коварный: TCP-соединение
устанавливается, но POST висит до таймаута (из хоста 2.6 с, из контейнера — 45 с
и обрыв). Лечится `make routing` — скрипт идемпотентный, удаляет эти маршруты и
оставляет в туннеле только подсети Telegram. **После любого переподключения VPN
нужно повторить.**

**`host.docker.internal` в Linux не резолвится сам.** Базовый образ задаёт
`SANDBOX_LOCAL_RUNTIME_URL=http://host.docker.internal`, и сервер строит из этого
адрес runtime-контейнера. Без `extra_hosts: host.docker.internal:host-gateway`
в `docker-compose.yml` получаем `Name or service not known`. Сетевую схему менять
не надо: runtime остаётся в `bridge` с публикацией портов, сервер достаёт его
через шлюз хоста. `network_mode: host` и кастомные сети — тупиковые попытки,
они в истории коммитов (`6a8a6c6`, `23f5eba`).

**Flutter нужен в runtime-образе, а не в образе сервера.** Сервер только
управляет агентом. Если поставить Flutter в `docker/openhands-flutter/Dockerfile`,
агент его не увидит.

**Команды агента идут через non-login shell**, который не читает
`/etc/profile.d`, поэтому `ENV PATH` из Dockerfile до них не доходит. Поэтому в
runtime-образе симлинки в `/usr/local/bin` плюс дублирование переменных в
`/etc/bash.bashrc`.

**Flutter 3.47 требует Android SDK 36**, не 35 — с 35 `flutter doctor` ругается.
Лицензии SDK принимаются на этапе сборки образа, иначе агент зависает на
интерактивном запросе.

**Модель — `openai/claude-sonnet-4.6`.** Префикс `openai/` говорит LiteLLM, что
endpoint OpenAI-совместимый. `gpt-4o` на provod.ai не существует, а
`claude-sonnet-5` отклоняет параметр `temperature`, который OpenHands посылает
всегда. Список моделей: `GET $OPENHANDS_LLM_BASE_URL/models`.

**Два оркестратора одновременно — Telegram даёт `409 Conflict`.** И осторожно с
`pkill -f "orchestrator.main"`: шаблон совпадает с самой вызывающей оболочкой и
убивает её. Правильно: `pkill -9 -f "[o]rchestrator[.]main"`.

**Вопросы агента автору из GitHub-задачи (грабли, на которые наступали).**
Механизм: агент переходит в `awaiting_user_input` (`MessageAction.args.
wait_for_response=true`) → оркестратор публикует вопрос комментарием в issue →
`poll_github` → `_poll_github_agent_question_answers` забирает первый комментарий
живого участника после вопроса и резолвит future. Грабли:

- В `_handle_agent_question` запись ожидания создавалась без ссылки `qinfo`, и
  в GitHub-ветке `qinfo["github_issue"] = num` падало с `NameError` — вопрос в
  issue публиковался, но ответы не собирались никогда (задача висела до
  таймаута). Ловится тестом `tests/test_agent_question_github.py`.
- `_wait_for_clarification` звал `await _run_openhands(...)` прямо в главном
  цикле: монитор задачи жил внутри цикла, и вопрос агента deadlock'ился —
  future мог разрешить только `poll_github` этого же цикла. Теперь запуск
  всегда в фоне (`_spawn`), плюс защита `_running_tasks` от двойного старта.
- После доставки ответа агент ещё какое-то время в `awaiting_user_input` со
  старым сообщением — без guard'а `answered_msg` монитор задаёт тот же вопрос
  повторно (дубли в issue/Telegram).
- `POST /api/conversations/{id}/message` возвращает `{"success": true}` и НЕ
  бросает исключений при ошибке — результат надо проверять; при сбое пробуем
  `start_conversation` (разговор могли погасить за время ожидания) и повторяем.
- Запись ожидания живёт в памяти. После рестарта `_resume_task` должен
  восстановить её с `github_issue` + `github_after_comment_id` (граница —
  последний комментарий бота, `GitHubAdapter.last_bot_comment_id`), иначе
  ответы после сбоя не собираются, а старый комментарий сойдёт за ответ.
- PyGithub синхронный: вызовы из async-кода уводим в `asyncio.to_thread`,
  иначе GitHub API на секунды замораживает весь event loop (Telegram, поллинг).
- OpenHands гасит простаивающий разговор через ~1 час idle, В ТОМ ЧИСЛЕ когда
  агент ждёт ответ пользователя (`stopped` после `awaiting_user_input`).
  Поэтому в `get_agent_state`: `completed=True` только для `finished`;
  отдельно возвращается `state_before_stop` — монитор, увидев «stopped во
  время ожидания ответа», будит разговор (`start_conversation`) и продолжает
  (лимит `OPENHANDS_MAX_STOP_RESTARTS`, по умолчанию 3). Раньше это состояние
  считалось «агент отчитался» — задача ложно завершалась без работы.
- `git commit` печатает «nothing to commit» в STDOUT, а не в stderr: задача
  без правок кода падала с пустой ошибкой «git commit: …» вместо штатного
  «Изменений нет — PR не требуется». Проверяются оба потока.

**Запуск в фоне через `setsid python3 ... &` из одной shell-команды молча не
стартует** — процесс умирает вместе с оболочкой. Нужен `setsid nohup ... &` и
проверка `pgrep -f "python3 -m orchestrator.main"` после.

**`/api/health` возвращает HTML фронтенда, не JSON.** Healthcheck ходит на
`http://localhost:3000/`.

**`[Errno 2] No such file or directory: '.openhands/setup.sh'`** в логах runtime —
это не ошибка, а штатная проверка опциональных файлов. Настоящие ошибки агента
видны не в логах контейнера, а в траектории разговора (см. ниже).

## Как смотреть, что происходит

```bash
tail -f /tmp/orchestrator.log                       # оркестратор
docker logs -f openhands-flutter                    # сервер (шумит GET /assets, GET /api/conversations)
docker logs -f $(docker ps -q --filter "name=openhands-runtime" | head -1)
```

`sudo` для docker не нужен — пользователь в группе `docker`.

Самое полезное — траектория разговора: там видно каждую команду агента, её вывод
и exit code.

```bash
cd /mnt/ssd/personal_worker && set -a && . ./.env && set +a
curl -s -H "Authorization: Bearer $OPENHANDS_API_KEY" \
  "http://localhost:3000/api/conversations/<CONV_ID>/trajectory" | python3 -m json.tool | less
```

`<CONV_ID>` берётся из поля `conversation_id` задачи в `state/orchestrator.sqlite`
или из имени runtime-контейнера.

Интерфейс Telegram (постоянное меню-кнопки + инлайн-клавиатура). Слэг-команды,
кроме `/start`, заменены кнопками меню (`ReplyKeyboardMarkup`); кнопка шлёт текст,
который маршрутизируется в ту же команду:

- `📊 Статус` (`/status`) — статус текущей задачи эмодзи, простые статусы трёх
  слоёв (оркестратор/OpenHands/runtime — 🟢/🔴/⚪), **цена работы** и **логи
  траектории** агента (реальные команды и exit-коды — самое информативное).
- `📋 Задачи` (`/tasks`) — все прошлые задачи (GitHub 🐙 + Telegram 💬) с
  результатами, ссылками на PR и ценой, пагинация; у каждой — карточка с кнопками
  «🔄 Перезапустить», «🧠 Модель», «📖 Логи».
- `🧠 Модель` (`/model`) — модель LLM **по умолчанию** (для новых задач).
- `🚫 Отмена` (`/cancel`), `❓ Помощь` (`/help`).

Выбор модели — из списка доступных (`GET $OPENHANDS_LLM_BASE_URL/models`):
шорт-лист (сильнейшая модель каждого семейства) + кнопка «📋 Все модели»
(пагинация). Модель можно задать и для **упавшей** задачи, затем перезапустить
её — ветка PR берётся с суффиксом `-r<attempt>`, чтобы не конфликтовать с прошлым
прогоном.

**Стоимость работы.** Считается как токены (из `llm_metrics` OpenHands:
`GET /api/conversations/{id}/events` → `llm_metrics.accumulated_token_usage`) ×
сохранённые цены модели, в ₽ (курс `rub_per_usd`, по умолчанию 82.8). Цены
(USD/токен из `GET /v1/models` → `pricing`) и метрики **SWE-бенчмарков** (из
отчёта «provod.ai → Flutter: цена/качество») лежат в БД (`model_prices`,
`model_benchmarks`); цены обновляются автоматически раз в 6 часов. Модуль —
`orchestrator/core/pricing.py`. Цена показывается в карточке задачи и `/status`.

**Вопросы агента (в процессе работы).** Когда модель ждёт ответа
(`awaiting_user_input`, сигнал `wait_for_response` в `MessageAction.args`),
оркестратор показывает вопрос пользователю: варианты ответа, извлечённые из
текста (нумерованные `1) 2) 3)`, буллиты `-`, буквы `A) B)`) — кнопками; если
пунктов нет, ответ — свободным текстом. Ответ уходит **в тот же разговор** через
`send_message` (не перезапуская задачу), после чего мониторинг продолжается.
Для GitHub-задач вопрос публикуется комментарием в issue, ответ — следующим
комментарием автора (номер варианта или свободный текст); регрессионные тесты —
`make test` (`tests/test_agent_question_github.py`).

Прибрать простаивающие runtime-контейнеры (безопасно, когда задача не идёт —
проверьте `/status`):

```bash
docker rm -f $(docker ps -aq --filter "name=openhands-runtime")
```

## Сеть

- Этот хост `192.168.88.15` (enp3s0, шлюз `192.168.88.1`), iMac `192.168.88.20`.
- VPN tun0, шлюз `10.8.0.1`. Через VPN идёт **только Telegram** (и реестры Docker).
- `api.provod.ai` = `158.160.9.95`, идёт через LAN, мимо VPN. Маршрут —
  конкретный `/32` через `enp3s0` (`deploy/setup_routing.sh`, шаг 3b): он
  специфичнее blanket `0.0.0.0/1` от OpenVPN и переживает переподключение VPN.
  Агент в runtime-контейнере ходит в provod.ai через хост-NAT → LAN, поэтому
  работает и без VPN.
- Реестры Docker — через зеркала в `/etc/docker/daemon.json` (там же
  `data-root: /mnt/ssd/docker`).

## API OpenHands (v0.35)

`POST /api/settings`, `POST /api/add-git-providers`,
`POST /api/conversations` (поля `initial_user_msg`, `repository`,
`selected_branch`), `POST /api/conversations/{id}/start`,
`GET /api/conversations/{id}`, `GET /api/conversations/{id}/trajectory`,
`GET /api/conversations/{id}/events` (у событий есть `llm_metrics` с
`accumulated_token_usage` — источник токенов для цены), `POST /api/conversations/{id}/message` (отправить ответ агенту, см. «Вопросы агента»). У провайдера также
`GET /v1/usage` (суммарная статистика) и `GET /v1/models` (цены).

GitHub App через PyGithub 2.5.0: `AppAuth` + `get_installation_auth()`, затем
`Github(auth=installation_auth)`. Не обращайтесь к `.token` до того, как
прикреплён requester — иначе исключение.

## Ограничения, заданные пользователем

- Все данные и файлы — на `/mnt/ssd` (системный диск 80 ГБ, свободно ~20 ГБ).
- Всё работает на одной машине (Arch Linux, Python 3.14). Второй машины нет.
- OpenHands — только в контейнере, на хост не ставить. `network_mode: host` не
  использовать.
- VPN и маршрутизация поднимаются автоматически из `make run`, без ручных шагов.
- Сборки и тесты Flutter выполняет модель внутри runtime-контейнера, а не
  раннеры на хосте.
- Бот сообщает о своём состоянии, говорит «занят» при занятости, умеет отменять
  задачу, при падении присылает лог в чат.

## Что дальше

1. Дать боту реальную задачу по `social_mind` и проследить
   `/tmp/orchestrator.log` + траекторию.
2. Подтвердить `flutter build apk` сквозным прогоном на новом runtime-образе.
3. Проверить создание PR и уведомление в Telegram.
4. Запустить `deploy/setup_imac.sh` на iMac и проверить путь iOS.
5. Мелочи: удалить неиспользуемый `openhands_venv/` (остаток попытки нативной
   установки — Python 3.14 ломает protobuf: `Metaclasses with custom tp_new are
   not supported`), прибрать мёртвые сети `personal_worker_oh-network` и
   `personal_worker_personal_worker_default`.
