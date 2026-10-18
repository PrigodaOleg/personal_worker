#!/usr/bin/env bash
# ============================================================
# Настройка маршрутизации: только Telegram через VPN, остальное через LAN
# Вызывается из Makefile перед запуском orchestrator
# ============================================================
set -euo pipefail

TELEGRAM_IP="149.154.166.110"
VPN_DEV="tun0"
LAN_DEV="enp3s0"
LAN_GW="192.168.88.1"

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

log_info "Настройка маршрутизации: Telegram через $VPN_DEV, остальное через $LAN_DEV"

# 1. Удаляем маршруты по умолчанию через VPN (OpenVPN добавляет 0.0.0.0/1 и 128.0.0.0/1)
ip route del 0.0.0.0/1 dev "$VPN_DEV" 2>/dev/null || true
ip route del 128.0.0.0/1 dev "$VPN_DEV" 2>/dev/null || true
log_info "Удалены маршруты по умолчанию через VPN"

# 2. Убеждаемся, что default route через LAN
ip route replace default via "$LAN_GW" dev "$LAN_DEV" metric 100 2>/dev/null || true
log_info "Default route через $LAN_GW ($LAN_DEV)"

# 3. Маршрут к API Telegram через VPN
ip route replace "$TELEGRAM_IP" via 10.8.0.1 dev "$VPN_DEV" 2>/dev/null || true
log_info "Маршрут к Telegram API ($TELEGRAM_IP) через $VPN_DEV"

# 4. Исключения для VPN серверов (чтобы соединение с VPN шло через LAN, а не через сам VPN)
# Разрешаем имена серверов из germany.ovpn
for vpn_host in gmrgwztvzi.de.jdfsd.space l8i4sb2gnv.de.klek.it.com; do
    vpn_ip=$(getent hosts "$vpn_host" | awk '{print $1}' | head -1)
    if [ -n "$vpn_ip" ]; then
        ip route replace "$vpn_ip" via "$LAN_GW" dev "$LAN_DEV" 2>/dev/null || true
        log_info "Исключение VPN сервера $vpn_host ($vpn_ip) через LAN"
    fi
done

# Также добавляем известные IP VPN серверов, если они есть в текущих маршрутах
for known_vpn_ip in 85.198.80.162 195.133.32.135; do
    if ip route get "$known_vpn_ip" 2>/dev/null | grep -q "dev $VPN_DEV"; then
        ip route replace "$known_vpn_ip" via "$LAN_GW" dev "$LAN_DEV" 2>/dev/null || true
        log_info "Перенаправлен известный VPN сервер $known_vpn_ip через LAN"
    fi
done

log_info "Маршрутизация настроена успешно"
ip route show | grep -E "default|telegram|tun0|enp3s0" || true
