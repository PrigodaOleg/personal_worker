#!/usr/bin/env bash
# Установка personal_worker как systemd-сервиса (24/7, авторестарт)
set -euo pipefail

USER_NAME="${USER:-oleg}"
PROJECT_DIR="/mnt/ssd/personal_worker"
SERVICE_FILE="$PROJECT_DIR/deploy/personal_worker.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "==> Проверка Docker..."
docker --version >/dev/null 2>&1 || { echo "Docker не найден"; exit 1; }

echo "==> Создание рабочих директорий..."
mkdir -p "$PROJECT_DIR/workspaces" "$PROJECT_DIR/state" "$PROJECT_DIR/config"

echo "==> Генерация SSH-ключа (если нет)..."
if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
    ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N "" -C "personal_worker@$(hostname)"
fi
echo "Публичный ключ для iMac (скопируйте на iMac в ~/.ssh/authorized_keys):"
cat "$HOME/.ssh/id_ed25519.pub"

echo "==> Копирование systemd-сервиса..."
sudo cp "$SERVICE_FILE" "$SYSTEMD_DIR/personal_worker.service"

echo "==> Перезагрузка systemd..."
sudo systemctl daemon-reload
sudo systemctl enable personal_worker.service
sudo systemctl start personal_worker.service

echo "==> Статус:"
sudo systemctl status personal_worker.service --no-pager || true

echo
echo "Готово. Сервис запущен и будет перезапускаться при сбоях и при загрузке ОС."
echo "Логи: journalctl -u personal_worker -f"
