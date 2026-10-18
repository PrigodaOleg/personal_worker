.PHONY: install run up down lint test fmt

VENV := .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run:
	$(PYTHON) -m orchestrator.main

up:
	docker compose up -d

down:
	docker compose down

lint:
	$(PIP) install ruff 2>/dev/null; $(VENV)/bin/ruff check orchestrator tests 2>/dev/null || echo "ruff not installed"

fmt:
	$(VENV)/bin/ruff format orchestrator tests 2>/dev/null || echo "ruff not installed"

test:
	$(VENV)/bin/pytest tests
