#!/usr/bin/env bash
# Регистрация ЭТОЙ машины как self-hosted GitHub Actions runner для personal_worker.
#
# Зачем: джобы валидации PR и деплоя (deploy/gha-*.yml) должны выполняться
# НА ЭТОЙ машине — здесь живёт сервис, сюда деплоим, здесь лежит .venv.
# У social_mind уже есть свой раннер (midioleg); этот — отдельный инстанс
# для personal_worker, со своим каталогом и systemd-сервисом.
#
# Токен регистрации (одноразовый) берётся в GitHub:
#   Repo personal_worker → Settings → Actions → Runners → New self-hosted runner
#   → Linux x64 → скопируйте значение --token из блока «Configure».
#
# Использование:
#   bash deploy/setup_deploy_runner.sh <RUNNER_TOKEN>
#   (или задайте RUNNER_TOKEN в окружении)
set -euo pipefail

REPO_URL="https://github.com/PrigodaOleg/personal_worker"
RUNNER_DIR="/mnt/ssd/personal_worker/actions-runner-deploy"
SERVICE_NAME="actions.runner.PrigodaOleg-personal_worker.deploy-runner"
SERVICE_USER="${USER:-oleg}"
TOKEN="${1:-${RUNNER_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
    echo "❌ Нужен одноразовый токен регистрации."
    echo "Использование: bash deploy/setup_deploy_runner.sh <RUNNER_TOKEN>"
    echo "Токен: GitHub → personal_worker → Settings → Actions → Runners → New self-hosted runner (Linux x64)"
    exit 1
fi

# Актуальная версия раннера
RUNNER_VER="$(curl -sf https://api.github.com/repos/actions/runner/releases/latest \
    | grep -oP '"tag_name":\s*"v\K[^"]+' || echo "2.325.0")"
echo "[runner] версия: v${RUNNER_VER}"

sudo mkdir -p "$RUNNER_DIR"
sudo chown "$SERVICE_USER":"$SERVICE_USER" "$RUNNER_DIR"
cd "$RUNNER_DIR"

if [[ ! -f ./config.sh ]]; then
    echo "[runner] скачиваю actions-runner..."
    curl -sL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VER}/actions-runner-linux-x64-${RUNNER_VER}.tar.gz"
    tar xzf runner.tar.gz
    rm -f runner.tar.gz
fi

# Метка personal-worker: деплой-джобы таргетятся в неё, чтобы не уйти
# на другой self-hosted раннер, если такой появится.
# --replace: перерегистрация при повторном запуске скрипта.
./config.sh --url "$REPO_URL" --token "$TOKEN" \
    --labels personal-worker --unattended --replace

# Ставим как systemd-сервис (автозапуск при загрузке, авторестарт).
sudo ./svc.sh install "$SERVICE_USER"
sudo ./svc.sh start
echo "[runner] готов: сервис ${SERVICE_NAME} запущен."
echo "[runner] статус: systemctl status ${SERVICE_NAME}"
