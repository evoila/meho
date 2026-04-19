# MEHO Makefile - Common development commands

.PHONY: help install dev-up dev-down test lint format clean

help:
	@echo "MEHO Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install     - Install dependencies"
	@echo "  make dev-up      - Start development environment"
	@echo "  make dev-down    - Stop development environment"
	@echo ""
	@echo "Development:"
	@echo "  make test        - Run all tests"
	@echo "  make test-unit   - Run unit tests only"
	@echo "  make test-watch  - Run tests in watch mode"
	@echo "  make lint        - Run linters"
	@echo "  make format      - Format code"
	@echo ""
	@echo "Database:"
	@echo "  make migrate     - Run database migrations"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean       - Remove generated files"

install:
	pip install -e ".[dev,test]"

dev-up:
	./scripts/dev-env.sh up

dev-down:
	./scripts/dev-env.sh down

test:
	./scripts/run-tests.sh

test-unit:
	./scripts/run-unit-tests.sh

test-integration:
	./scripts/run-integration-tests.sh

test-watch:
	./scripts/watch-tests.sh

lint:
	./scripts/lint.sh

format:
	ruff format .
	ruff check --fix .

migrate:
	./scripts/run-migrations-monolith.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

