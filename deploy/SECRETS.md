# Секреты сборки (Firebase / CLOSERS_) → воркер

## Зачем

Проект `social_mind` собирается в GitHub Actions (`dart.yml`) с секретами
`CLOSERS_*` (Firebase-конфиг): они подставляются в `google-services.json` и в
`--dart-define` при сборке APK/web. Секреты лежат **только в GitHub** — их
значения **нельзя прочитать по API** (API отдаёт лишь имён). Единственный способ
вытащить их наружу — **workflow-джоба**.

Чтобы воркер (наша машина) собирал Flutter с тем же конфигом, что и CI, секреты
нужно прокинуть на машину. Выбран механизм **self-hosted runner**.

## Как это работает

```
GitHub (секреты CLOSERS_*)
        │  workflow-джоба runs-on: self-hosted
        ▼  (выполняется НА НАШЕЙ машине)
/mnt/ssd/personal_worker/secrets/
        ├── build.env              ← KEY=VALUE для --dart-define
        └── google-services.json   ← для Android-сборки
        │
        ▼  оркестратор копирует в workspace перед запуском агента
/workspace/social_mind/domains/client/
        ├── build.env
        └── android/app/google-services.json
```

- Секреты остаются в GitHub (единый источник правды, ротация = обновить секрет).
- На машину они попадают только внутри джобы и кладутся в `secrets/` (chmod 600).
- Оркестратор при старте задачи копирует их в workspace (`_inject_build_secrets`)
  и пишет агенту, как передать `--dart-define`.

## Одноразовая настройка (нужны ваши действия в GitHub)

### 1. Зарегистрировать self-hosted runner

1. GitHub → репозиторий `social_mind` → **Settings → Actions → Runners →
   New self-hosted runner**.
2. В блоке «Configure using...» скопируйте значение `--token ...`
   (одноразовый токен регистрации).
3. На нашей машине:

   ```bash
   cd /mnt/ssd/personal_worker
   bash deploy/setup_runner.sh <RUNNER_TOKEN>
   ```

   Скрипт скачает раннер, зарегистрирует его (метка `personal-worker`) и поставит
   как systemd-сервис (работает после перезагрузки).

### 2. Положить workflow, отдающий секреты

1. Скопируйте `deploy/sync-build-secrets.yml` в репозиторий `social_mind` как
   `.github/workflows/sync-build-secrets.yml`.
2. Смержите в `main` (для `workflow_dispatch` файл должен быть на основной ветке).

## Дальше — автоматически

- Workflow `Sync build secrets to worker` кладёт секреты в `secrets/` **каждые
  6 часов** (по расписанию) и держит файл свежим.
- Можно обновить вручную: GitHub → **Actions → Sync build secrets to worker →
  Run workflow** (или `make secrets`, чтобы посмотреть статус файлов).
- Оркестратор сам подхватывает `secrets/` при старте задачи.

## Если раннер/секреты не настроены

Всё работает и без них: `_inject_build_secrets` просто не найдёт файлов, сборка
продолжится без Firebase-конфига (для валидации «код компилируется + тесты
зелёные» обычно достаточно). Полный функциональный APK (с Firebase) требует
секретов.

## Файлы

- `deploy/sync-build-secrets.yml` — workflow, отдающий секреты (в `social_mind`).
- `deploy/setup_runner.sh` — регистрация self-hosted runner.
- `orchestrator/core/scheduler.py::_inject_build_secrets` — копирование секретов
  в workspace перед сборкой.
