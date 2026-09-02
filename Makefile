.PHONY: install test lint fmt check audit cov fixtures record run-weekly run-daily run-monthly backlog ready

install:
	python -m pip install -e ".[dev]"

test:
	pytest -q -m 'not network'

lint:
	ruff check src tests scripts

fmt:
	ruff format src tests scripts

check: lint test

cov:                ## branch coverage; fails under the floor
	pytest -q --cov=finder --cov-branch --cov-report=term-missing --cov-fail-under=88

audit:              ## break the code on purpose; every mutation must be caught
	python scripts/audit_tests.py

run-weekly:
	python -m finder.run weekly

run-daily:
	python -m finder.run daily

run-monthly:
	python -m finder.run monthly

fixtures:           ## list recorded provider responses
	python scripts/record_fixtures.py --list

record:             ## record the three verified seed routes (needs API keys, hits the network)
	python scripts/record_fixtures.py --seeds

backlog:            ## reload plan/backlog.yaml into beads (idempotent)
	python scripts/load_backlog.py

ready:              ## what can be worked on right now
	bd ready
