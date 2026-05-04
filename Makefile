.PHONY: install dev-install run lint typecheck test fmt

install:
	pip install -r requirements.txt

dev-install:
	pip install -r requirements-dev.txt

run:
	python -m zeenova_bot.main

lint:
	ruff check zeenova_bot tests

fmt:
	ruff format zeenova_bot tests
	ruff check --fix zeenova_bot tests

typecheck:
	mypy zeenova_bot

test:
	pytest -q
