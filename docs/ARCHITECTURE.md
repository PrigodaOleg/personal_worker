# Архитектура personal_worker

## Поток задачи

1. **Приём:** GitHub Issue (с меткой, например `personal-worker`) или команда в Telegram.
2. **Валидация:** задача содержит описание что нужно сделать в Flutter-проекте.
3. **Уточнения (если нужно):** Orchestrator спрашивает пользователя в Telegram / комментарии к Issue.
   - Статус задачи: `clarifying`.
   - После ответа — `ready`.
4. **Выполнение:** OpenHands получает инструкцию, разворачивает Flutter-проект в изолированной рабочей директории, пишет код.
5. **Тесты и сборки:**
   - `flutter test` — локально.
   - `flutter build linux`, `flutter build web`, `flutter build apk` — локально (Docker с Flutter SDK).
   - `flutter build ios` + iOS тесты — на iMac по SSH.
6. **PR:** при успехе создаётся ветка и PR в GitHub.
7. **Уведомление:** результат приходит в Telegram и/или комментарий к Issue.

## Разделение обязанностей по платформам

| Платформа | Где собирается | Где тестируется | Инструмент |
|-----------|----------------|-----------------|------------|
| Linux     | этот ПК        | этот ПК         | Docker + Flutter SDK |
| Web       | этот ПК        | этот ПК         | Docker + Flutter SDK |
| Android   | этот ПК        | этот ПК         | Docker + Flutter SDK + Android SDK/NDK |
| iOS       | iMac (SSH)     | iMac (SSH)      | Flutter SDK на macOS, Xcode |

## Требования к iMac

- macOS с Xcode и Flutter SDK
- SSH включён (System Settings → General → Sharing → Remote Login)
- Доступен из той же подсети (192.168.88.0/24)
- Ключ SSH от рабочего ПК должен быть добавлен в `~/.ssh/authorized_keys` на iMac
- Flutter и `xcodebuild` должны быть в PATH

## Состояние

Все задачи хранятся в SQLite-файле (`state/orchestrator.sqlite`). Это позволяет прервать и возобновить работу без потери прогресса.
