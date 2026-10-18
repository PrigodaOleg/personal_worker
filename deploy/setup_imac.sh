#!/usr/bin/env bash
# ============================================================
# Скрипт настройки iMac для personal_worker (сборка и тесты iOS)
# Скопируйте на iMac и запустите:  bash setup_imac.sh
# IP iMac: 192.168.88.20
# ============================================================
set -euo pipefail

USER_NAME="${USER:-$(whoami)}"
WORK_DIR="/Users/${USER_NAME}/personal_worker"
SSH_KEY_PATH="${HOME}/.ssh/id_ed25519"

echo "============================================================"
echo "  Настройка iMac для personal_worker (сборка iOS)"
echo "  Пользователь: ${USER_NAME}"
echo "  Рабочая директория: ${WORK_DIR}"
echo "============================================================"

# ------------------------------------------------------------
# 1. Xcode Command Line Tools
# ------------------------------------------------------------
echo "=> Проверка Xcode Command Line Tools..."
if ! xcode-select -p &>/dev/null; then
    echo "   Установка Xcode Command Line Tools..."
    xcode-select --install
    echo "   Дождитесь завершения установки и перезапустите скрипт."
    exit 1
else
    echo "   OK: $(xcode-select -p)"
fi

# ------------------------------------------------------------
# 2. Homebrew
# ------------------------------------------------------------
echo "=> Проверка Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "   Установка Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Для Apple Silicon добавить brew в PATH
    if [[ -d "/opt/homebrew/bin" ]]; then
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "${HOME}/.zprofile"
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
else
    echo "   OK: $(brew --prefix)"
fi

# ------------------------------------------------------------
# 3. Flutter SDK
# ------------------------------------------------------------
echo "=> Проверка Flutter..."
if ! command -v flutter &>/dev/null; then
    echo "   Установка Flutter через Homebrew..."
    brew install --cask flutter || true
    # Если cask не сработал — ставим вручную
    if ! command -v flutter &>/dev/null; then
        echo "   Ручная установка Flutter..."
        git clone https://github.com/flutter/flutter.git -b stable "${HOME}/flutter"
        echo 'export PATH="$HOME/flutter/bin:$PATH"' >> "${HOME}/.zprofile"
        export PATH="${HOME}/flutter/bin:$PATH"
    fi
else
    echo "   OK: $(flutter --version | head -1)"
fi

echo "=> flutter doctor (проверка окружения)..."
flutter doctor || true

# ------------------------------------------------------------
# 4. SSH (для подключения от этого ПК)
# ------------------------------------------------------------
echo "=> Настройка SSH..."
if [ ! -f "${SSH_KEY_PATH}" ]; then
    echo "   Генерация SSH-ключа на iMac..."
    ssh-keygen -t ed25519 -f "${SSH_KEY_PATH}" -N "" -C "imac@$(hostname -s)"
else
    echo "   SSH-ключ уже существует: ${SSH_KEY_PATH}"
fi

# Включить Remote Login (System Settings -> General -> Sharing -> Remote Login)
echo "=> Включение Remote Login (SSH)..."
sudo systemsetup -setremotelogin on 2>/dev/null || {
    echo "   ВНИМАНИЕ: не удалось включить Remote Login автоматически."
    echo "   Включите вручную: Системные настройки -> Общие -> Поделиться -> Удалённый вход"
}

# Разрешить полный дисковый доступ для sshd (если нужна сборка в ~/personal_worker)
# Обычно не требуется, так как работа в домашней директории.

# ------------------------------------------------------------
# 5. Рабочая директория
# ------------------------------------------------------------
echo "=> Создание рабочей директории ${WORK_DIR}..."
mkdir -p "${WORK_DIR}"
chmod 755 "${WORK_DIR}"

# ------------------------------------------------------------
# 6. Информация для пользователя
# ------------------------------------------------------------
echo ""
echo "============================================================"
echo "  НАСТРОЙКА ЗАВЕРШЕНА"
echo "============================================================"
echo ""
echo "IP iMac: $(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || 'неизвестно')"
echo "Пользователь: ${USER_NAME}"
echo "SSH-ключ (публичный):"
cat "${SSH_KEY_PATH}.pub"
echo ""
echo "Скопируйте этот публичный ключ на рабочий ПК (192.168.88.17)"
echo "в файл ~/.ssh/id_ed25519.pub (или добавьте в ~/.ssh/authorized_keys на этом ПК)."
echo ""
echo "Рабочая директория на iMac: ${WORK_DIR}"
echo "Впишите в .env на рабочем ПК:"
echo "  IMAC_HOST=192.168.88.20"
echo "  IMAC_USER=${USER_NAME}"
echo "  IMAC_WORK_DIR=${WORK_DIR}"
echo ""
echo "Проверьте подключение с рабочего ПК:"
echo "  ssh -i ~/.ssh/id_ed25519 ${USER_NAME}@192.168.88.20 'flutter doctor'"
echo ""
