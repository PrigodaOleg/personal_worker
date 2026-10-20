#!/usr/bin/env bash
# Регистрация ЭТОЙ машины как self-hosted GitHub Actions runner для social_mind.
#
# Зачем: чтобы workflow-джобы (например, deploy/sync-build-secrets.yml) выполнялись
# на нашей машине и могли забрать секреты из GitHub (значения секретов доступны
# ТОЛЬКО внутри джобы — по API их не прочитать).
#
# Токен регистрации (одноразовый) берётся в GitHub:
#   Repo social_mind → Settings → Actions → Runners → New self-hosted runner
#   → раздел «Configure using...» → скопируйте значение --token ...
#
# Использование:
#   bash deploy/setup_runner.sh <RUNNER_TOKEN>
#   (или задайте RUNNER_TOKEN в окружении)
set -euo pipefail

REPO_URL="https://github.com/PrigodaOleg/social_mind"
RUNNER_DIR="/mnt/ssd/personal_worker/actions-runner"
SERVICE_USER="oleg"
TOKEN="${1:-${RUNNER_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
    echo "❌ Нужен токен регистрации."
    echo "Использование: bash deploy/setup_runner.sh <RUNNER_TOKEN>"
    echo "Токен: GitHub → social_mind → Settings → Actions → Runners → New self-hosted runner"
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

# Регистрируем (метка personal-worker — чтобы джобы могли целиться в эту машину).
# --replace — если раннер уже был зарегистрирован, перерегистрируем.
./config.sh --url "$REPO_URL" --token "$TOKEN" \
    --labels personal-worker --unattended --replace

# Ставим как systemd-сервис, чтобы раннер поднимался после перезагрузки ОС.
sudo ./svc.sh install "$SERVICE_USER"
sudo ./svc.sh start
sudo ./svc.sh status

echo
echo "✅ Runner зарегистрирован и запущен как сервис."
echo "   Теперь workflow с 'runs-on: self-hosted' будет выполняться на этой машине."
echo "   Следующий шаг: положить deploy/sync-build-secrets.yml в social_mind/.github/workflows/"
echo "   и смержить в main."
