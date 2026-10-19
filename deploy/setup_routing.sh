#!/usr/bin/env bash
# ============================================================
# Настройка маршрутизации:
#   Telegram + Docker registry (OpenHands runtime) через VPN
#   Всё остальное через LAN
# Вызывается из Makefile перед запуском orchestrator
# ============================================================
set -euo pipefail

# Официальные подсети Telegram (api.telegram.org балансируется по многим адресам,
# поэтому одного IP недостаточно).
TELEGRAM_NETS=(
    "149.154.160.0/20"
    "91.108.4.0/22"
    "91.108.8.0/22"
    "91.108.12.0/22"
    "91.108.16.0/22"
    "91.108.56.0/22"
)
VPN_DEV="tun0"
VPN_GW="10.8.0.1"
LAN_DEV="enp3s0"
LAN_GW="192.168.88.1"

# Docker registry, которые должны ходить через VPN (ghcr.io может быть недоступен напрямую)
GHCR_HOST="ghcr.io"
DOCKER_HUB_HOST="registry-1.docker.io"
DOCKER_CLOUDFLARE_HOST="production.cloudflare.docker.com"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[routing]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[routing]${NC} $1"; }
log_err()  { echo -e "${RED}[routing]${NC} $1"; }

# Проверяем, что tun0 существует
if ! ip addr show "$VPN_DEV" &>/dev/null; then
    log_err "Интерфейс $VPN_DEV не найден. Убедитесь, что OpenVPN запущен."
    exit 1
fi

log_info "Настройка маршрутизации: Telegram + Docker registry через $VPN_DEV, остальное через $LAN_DEV"

# 1. Удаляем маршруты по умолчанию через VPN (OpenVPN добавляет 0.0.0.0/1 и 128.0.0.0/1)
ip route del 0.0.0.0/1 dev "$VPN_DEV" 2>/dev/null || true
ip route del 128.0.0.0/1 dev "$VPN_DEV" 2>/dev/null || true
log_info "Удалены маршруты по умолчанию через VPN"

# 2. Убеждаемся, что default route через LAN
ip route replace default via "$LAN_GW" dev "$LAN_DEV" metric 100 2>/dev/null || true
log_info "Default route через $LAN_GW ($LAN_DEV)"

# 3. Маршруты к подсетям Telegram через VPN
for tg_net in "${TELEGRAM_NETS[@]}"; do
    ip route replace "$tg_net" via "$VPN_GW" dev "$VPN_DEV" 2>/dev/null || true
    log_info "Маршрут к Telegram $tg_net через $VPN_DEV"
done

# 3b. LLM (provod.ai) — через LAN, мимо VPN. Конкретный /32-маршрут переживает
# переподключение VPN: OpenVPN возвращает blanket 0.0.0.0/1, но /32 специфичнее
# и всегда побеждает. Агент в runtime-контейнере ходит в provod.ai через хост (NAT),
# поэтому этого маршрута достаточно, чтобы и контейнер шёл мимо VPN.
LLM_HOSTS=( "api.provod.ai" )
for llm_host in "${LLM_HOSTS[@]}"; do
    while read -r llm_ip; do
        [ -z "$llm_ip" ] && continue
        ip route replace "$llm_ip/32" via "$LAN_GW" dev "$LAN_DEV" 2>/dev/null || true
        log_info "LLM $llm_host ($llm_ip/32) через $LAN_DEV (без VPN)"
    done < <(getent ahostsv4 "$llm_host" 2>/dev/null | awk '{print $1}' | sort -u)
done

# 4. Исключения для VPN серверов (чтобы соединение с VPN шло через LAN, а не через сам VPN)
for vpn_host in gmrgwztvzi.de.jdfsd.space l8i4sb2gnv.de.klek.it.com; do
    vpn_ip=$(getent hosts "$vpn_host" 2>/dev/null | awk '{print $1}' | head -1 || true)
    if [ -n "$vpn_ip" ]; then
        ip route replace "$vpn_ip" via "$LAN_GW" dev "$LAN_DEV" 2>/dev/null || true
        log_info "Исключение VPN сервера $vpn_host ($vpn_ip) через LAN"
    else
        log_warn "Не удалось разрешить имя $vpn_host, пропускаем"
    fi
done

# Также добавляем известные IP VPN серверов, если они есть в текущих маршрутах
for known_vpn_ip in 85.198.80.162 195.133.32.135; do
    if ip route get "$known_vpn_ip" 2>/dev/null | grep -q "dev $VPN_DEV"; then
        ip route replace "$known_vpn_ip" via "$LAN_GW" dev "$LAN_DEV" 2>/dev/null || true
        log_info "Перенаправлен известный VPN сервер $known_vpn_ip через LAN"
    fi
done

# 5. Маршруты к Docker registry через VPN (для OpenHands runtime и других образов)
for host in "$GHCR_HOST" "$DOCKER_HUB_HOST" "$DOCKER_CLOUDFLARE_HOST"; do
    host_ip=$(getent hosts "$host" 2>/dev/null | awk '{print $1}' | head -1 || true)
    if [ -n "$host_ip" ]; then
        ip route replace "$host_ip" via "$VPN_GW" dev "$VPN_DEV" 2>/dev/null || true
        log_info "Маршрут к Docker registry $host ($host_ip) через $VPN_DEV"
    else
        log_warn "Не удалось разрешить имя $host, пропускаем"
    fi
done

# 6. Проверка: всё, кроме Telegram и registry, должно идти через LAN.
# OpenVPN любит возвращать 0.0.0.0/1 и 128.0.0.0/1 при переподключении,
# что перехватывает в том числе трафик к LLM (api.provod.ai) и ломает агента.
verify_via_lan() {
    local host="$1"
    local ip
    ip=$(getent hosts "$host" 2>/dev/null | awk '{print $1}' | head -1 || true)
    [ -z "$ip" ] && { log_warn "Не удалось разрешить $host, проверка пропущена"; return 0; }
    if ip route get "$ip" 2>/dev/null | grep -q "dev $VPN_DEV"; then
        log_err "$host ($ip) всё ещё идёт через $VPN_DEV — split-routing не применился"
        return 1
    fi
    log_info "$host ($ip) идёт через LAN — ок"
}

verify_via_lan "api.provod.ai" || true

log_info "Маршрутизация настроена успешно"
echo "=== Текущие маршруты ==="
ip route show | grep -E "default|tun0|enp3s0" || true
