# Архитектура personal_worker

## Поток задачи

1. **Приём:** GitHub Issue (метка `aitask`) или сообщение в Telegram.
2. **Валидация:** достаточно ли информации для запуска.
3. **Уточнения (если нужно):** Orchestrator спрашивает пользователя.
   - GitHub → комментарий к issue; Telegram → личному сообщению автору.
   - Ожидание ответа: 3 часа.
4. **Выполнение:** OpenHands-агент (в своём контейнере с Flutter SDK) получает
   инструкцию, клонирует репозиторий, пишет код и **сам запускает**
   `flutter test`, `flutter build linux`, `flutter build web`,
   `flutter build apk` внутри контейнера. При ошибках исправляет код.
5. **iOS:** после успешного прохождения Linux/Web/Android код собирается и
   тестируется на iMac по SSH (`flutter build ios`, `flutter test`).
6. **PR:** создаётся ветка, коммит, пуш и PR в `PrigodaOleg/social_mind`.
7. **Уведомление:** результат приходит автору задачи (комментарий к issue /
   личное сообщение в Telegram).

## Разделение обязанностей по платформам

| Платформа | Где собирается | Где тестируется | Как запускается |
|-----------|----------------|-----------------|-----------------|
| Linux     | контейнер OpenHands | контейнер OpenHands | агент сам: `flutter build linux` |
| Web       | контейнер OpenHands | контейнер OpenHands | агент сам: `flutter build web` |
| Android   | контейнер OpenHands | контейнер OpenHands | агент сам: `flutter build apk` |
| iOS       | iMac (SSH)     | iMac (SSH)      | orquestaтор через `IMacAdapter` |

## Требования к iMac

- macOS с Xcode и Flutter SDK
- SSH включён (System Settings → General → Sharing → Remote Login)
- Доступен из той же подсети (192.168.88.0/24)
- Публичный ключ этого ПК в `~/.ssh/authorized_keys` на iMac

## Состояние

Все задачи хранятся в SQLite-файле (`state/orchestrator.sqlite`). Это позволяет
прервать и возобновить работу без потери прогресса (в т.ч. после перезагрузки ОС).

## Образ OpenHands

`docker/openhands-flutter/Dockerfile` — официальный образ OpenHands 0.35 +
установленный Flutter SDK. Собирается через `docker compose build`.
