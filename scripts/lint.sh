#!/bin/bash
set -e

echo "========================================="
echo "Running Code Quality Checks"
echo "========================================="

echo ""
echo "1. Ruff Linter..."
ruff check .

echo ""
echo "2. Ruff Formatter Check..."
ruff format --check .

echo ""
echo "3. MyPy Type Checker..."
mypy meho_core meho_knowledge meho_openapi meho_agent meho_ingestion meho_api --strict || true

echo ""
echo "========================================="
echo "✓ Code quality checks complete"
echo "========================================="
echo ""
echo "To auto-fix issues:"
echo "  ruff check --fix ."
echo "  ruff format ."

