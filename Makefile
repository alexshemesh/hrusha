SHELL := /bin/bash
PYTHON ?= python3.12
VENV := .venv

test:
	pytest .

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

prepare:
	pip install -e '.[dev]'

venv:
	$(PYTHON) -m venv $(VENV)
	@echo "run: source $(VENV)/bin/activate && make prepare hooks"

hooks:
	install -m 0755 scripts/githooks/pre-commit .git/hooks/pre-commit
	@echo "gitleaks pre-commit hook installed"

leaks:
	gitleaks git --redact

.PHONY: test lint format prepare venv hooks leaks
