.PHONY: install run up down lint test fmt vpn-up routing vpn-status runtime-image openhands-up

VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python

OVPN_CONFIG := ~/.config/germany.ovpn

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

# Запуск OpenVPN (если tun0 не существует)
vpn-up:
	@if ! ip addr show tun0 &>/dev/null; then \
		echo "[vpn] Запуск OpenVPN..."; \
		sudo openvpn --config $(OVPN_CONFIG) --daemon --log /tmp/openvpn.log; \
		sleep 5; \
		if ! ip addr show tun0 &>/dev/null; then \
			echo "[vpn] Ошибка: tun0 не создан. Проверьте /tmp/openvpn.log"; \
			exit 1; \
		fi; \
		echo "[vpn] OpenVPN запущен (tun0 активен)"; \
	else \
		echo "[vpn] OpenVPN уже запущен (tun0 активен)"; \
	fi

# Настройка маршрутизации: только Telegram через VPN, остальное через LAN
routing: vpn-up
	sudo bash deploy/setup_routing.sh

# Образ дочернего runtime-контейнера с Flutter.
# Агент выполняет команды именно в нём, поэтому Flutter нужен здесь,
# а не в образе сервера OpenHands.
runtime-image:
	sudo docker build -t personal_worker-runtime-flutter:latest \
		-f docker/runtime-flutter/Dockerfile docker/runtime-flutter

# Поднять OpenHands (сервер) после сборки runtime-образа
openhands-up: runtime-image
	sudo docker compose up -d --build
	@echo "[openhands] ожидание готовности..."
	@for i in $$(seq 1 30); do \
		if curl -sf -m 3 -o /dev/null http://localhost:3000/; then \
			echo "[openhands] готов на http://localhost:3000"; exit 0; \
		fi; \
		sleep 2; \
	done; \
	echo "[openhands] не ответил за 60с, смотрите: sudo docker logs openhands-flutter"; exit 1

# Запуск оркестратора (VPN, маршрутизация и OpenHands поднимаются сами)
run: routing openhands-up
	$(PYTHON) -m orchestrator.main

up: openhands-up

down:
	sudo docker compose down

vpn-status:
	@echo "=== Сетевые интерфейсы ==="
	ip addr show tun0 2>/dev/null || echo "tun0 не найден"
	ip route show | grep -E "default|tun0|enp3s0" || true
	@echo "=== Проверка Telegram API ==="
	curl -s -m 5 -o /dev/null -w "%{http_code}\n" https://api.telegram.org 2>&1 || echo "Telegram API недоступен"

lint:
	$(PIP) install ruff 2>/dev/null; $(VENV)/bin/ruff check orchestrator tests 2>/dev/null || echo "ruff not installed"

fmt:
	$(VENV)/bin/ruff format orchestrator tests 2>/dev/null || echo "ruff not installed"

test:
	$(VENV)/bin/pytest tests
