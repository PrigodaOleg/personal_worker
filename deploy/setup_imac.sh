#!/usr/bin/env bash
# ============================================================
# Настройка iMac для personal_worker (сборка и тесты iOS)
#
# Запускать НА iMac от администратора (один раз):
#   bash setup_imac.sh
#
# Скрипт делает всё, что делалось вручную при настройке:
#   1. Отдельный пользователь pwworker (стандартный, БЕЗ sudo) —
#      от него оркестратор гоняет сборки.
#   2. SSH-доступ: ключ воркера, разрешение SSH для pwworker
#      (группа com.apple.access_ssh — частая причина "Connection closed").
#   3. Xcode: лицензия + first launch (iOS SDK).
#   4. Сон отключён, автозапуск после сбоя питания.
#   5. Homebrew + CocoaPods (системный Ruby 2.6 слишком стар для гемов).
#   6. Flutter SDK (stable) в /Users/pwworker/flutter.
#   7. PATH в ~/.zshenv — чтобы команды работали при НЕИНТЕРАКТИВНОМ ssh
#      (оркестратор дёргает именно так).
#
# Переменные (поправь при необходимости):
IMAC_SSH_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID73Pt4o4ND4YFmG1jRkRVky9nHHkW/7NrMVXr2H7mAJ personal_worker->imac-pwworker'
WORKER_USER="${WORKER_USER:-pwworker}"
# ============================================================
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Запусти от администратора: sudo bash $0"
    exit 1
fi

echo "== 1. Пользователь для сборок: ${WORKER_USER} =="
if id "${WORKER_USER}" &>/dev/null; then
    echo "   уже есть"
else
    echo "   Задай пароль нового пользователя в запросе ниже:"
    sysadminctl -addUser "${WORKER_USER}" -home "/Users/${WORKER_USER}"
fi
chown -R "${WORKER_USER}":staff "/Users/${WORKER_USER}"
chmod 755 "/Users/${WORKER_USER}"

echo "== 2. SSH-ключ и разрешение на вход =="
install -d -m 700 -o "${WORKER_USER}" -g staff "/Users/${WORKER_USER}/.ssh"
printf '%s\n' "${IMAC_SSH_PUBKEY}" > "/Users/${WORKER_USER}/.ssh/authorized_keys"
chown "${WORKER_USER}":staff "/Users/${WORKER_USER}/.ssh/authorized_keys"
chmod 600 "/Users/${WORKER_USER}/.ssh/authorized_keys"
# Без этой группы sshd молча рвёт соединение ("Connection closed").
dscl . -append /Groups/com.apple.access_ssh GroupMembership "${WORKER_USER}" 2>/dev/null || true
systemsetup -setremotelogin on

echo "== 3. Xcode =="
xcode-select -p
xcodebuild -license accept
xcodebuild -runFirstLaunch || true
xcodebuild -showsdks | grep -i iphone || echo "   ⚠️ iOS SDK не найден — проверь установку Xcode"

echo "== 4. Сон и автозапуск =="
pmset -a sleep 0 disablesleep 1 autorestart 1 womp 1

echo "== 5. Homebrew + CocoaPods =="
if [[ ! -x /opt/homebrew/bin/brew ]]; then
    su - "${WORKER_USER}" -c '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' || true
fi
# Системный Ruby 2.6 не тянет свежие гемы — CocoaPods ставим из Homebrew.
su - "${WORKER_USER}" -c 'export PATH=/opt/homebrew/bin:$PATH; brew install cocoapods' || true

echo "== 6. Flutter (stable) =="
su - "${WORKER_USER}" -c 'test -d ~/flutter || git clone -b stable --depth 1 https://github.com/flutter/flutter.git ~/flutter'
ZSHENV="/Users/${WORKER_USER}/.zshenv"
grep -q '/opt/homebrew/bin' "${ZSHENV}" 2>/dev/null || \
    printf '%s\n' \
        'export PATH="/opt/homebrew/bin:$HOME/flutter/bin:$PATH"' \
        'export LANG=en_US.UTF-8' \
        'export LC_ALL=en_US.UTF-8' >> "${ZSHENV}"
chown "${WORKER_USER}":staff "${ZSHENV}"

echo "== 7. Проверка (неинтерактивно, как у оркестратора) =="
su - "${WORKER_USER}" -c 'zsh -c "flutter --version | head -2; pod --version"' || true

echo ""
echo "============================================================"
echo "  НАСТРОЙКА ЗАВЕРШЕНА"
echo "============================================================"
echo "IP iMac: $(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || 'неизвестно')"
echo ""
echo "Впиши в .env на рабочем ПК:"
echo "  IMAC_HOST=<IP iMac>"
echo "  IMAC_USER=${WORKER_USER}"
echo "  IMAC_SSH_KEY=~/.ssh/id_ed25519_pwworker"
echo "  IMAC_WORK_DIR=/Users/${WORKER_USER}/personal_worker"
echo ""
echo "Проверка с рабочего ПК:"
echo "  ssh -i ~/.ssh/id_ed25519_pwworker ${WORKER_USER}@<IP iMac> 'flutter doctor'"
