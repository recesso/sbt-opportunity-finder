.PHONY: install test lint fmt check run-weekly run-daily run-monthly backlog ready

install:
	python -m pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests scripts

fmt:
	ruff format src tests scripts

check: lint test

run-weekly:
	python -m finder.run weekly

run-daily:
	python -m finder.run daily

run-monthly:
	python -m finder.run monthly

backlog:            ## reload plan/backlog.yaml into beads (idempotent)
	python scripts/load_backlog.py

ready:              ## what can be worked on right now
	bd ready
