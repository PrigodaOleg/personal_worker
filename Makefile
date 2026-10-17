.PHONY: install run up down lint test fmt

install:
	pip install -r requirements.txt

run:
	python -m orchestrator.main

up:
	docker compose up -d

down:
	docker compose down

lint:
	ruff check orchestrator tests 2>/dev/null || echo "ruff not installed"

fmt:
	ruff format orchestrator tests 2>/dev/null || echo "ruff not installed"

test:
	pytest tests
