# Настройка GitHub App

Необходимо для аутентификации в GitHub (вместо PAT).

## 1. Создать GitHub App
1. Перейдите в https://github.com/settings/apps/new
2. **GitHub App name:** `personal-worker`
3. **Homepage URL:** `http://192.168.88.17:8080`
4. **Webhook URL:** (оставить пустым, будем использовать polling)
5. **Repository permissions:**
   - Contents: Read & write (клонирование, создание веток, PR)
   - Issues: Read & write (чтение issues, комментарии)
   - Pull requests: Read & write (создание/изменение/удаление PR)
   - Actions: Read (запуск CI/CD)
6. **Subscribe to events:** Issues, Pull requests (опционально)
7. Нажмите "Create GitHub App"

## 2. Скачать приватный ключ
На странице приложения нажмите "Generate a private key" — скачается `.pem` файл.
Положите его в:
```
/mnt/ssd/personal_worker/config/social_mind_app_private_key.pem
```

## 3. Получить ID приложения и Installation ID
- **App ID:** показана на странице приложения
- **Installation ID:** установите приложение на репозиторий `PrigodaOleg/social_mind`, затем ID можно получить через API или из URL при установке

## 4. Заполнить .env
```
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=/mnt/ssd/personal_worker/config/social_mind_app_private_key.pem
GITHUB_APP_INSTALLATION_ID=789012
```

## 5. Установить PyJWT
```bash
pip install pyjwt
```
