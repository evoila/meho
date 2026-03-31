#!/usr/bin/env bash
#
# Run critical tests (smoke + contract tests)
# These are fast tests that catch major breakages
# Should run in <30 seconds
#
# Usage:
#   ./scripts/run-critical-tests.sh          # Run all critical tests
#   ./scripts/run-critical-tests.sh --fast   # Skip coverage
#

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

FAST_MODE=false
if [[ "${1:-}" == "--fast" ]]; then
    FAST_MODE=true
fi

echo "🔍 Running Critical Tests (Smoke + Contract)"
echo "=============================================="
echo ""

# Activate virtual environment if it exists
if [[ -f ".venv/bin/activate" ]]; then
    source .venv/bin/activate
fi

# Run smoke tests (import validation, config, dependencies)
echo "1️⃣  Running Smoke Tests..."
if $FAST_MODE; then
    pytest tests/smoke/ -v --no-cov --tb=short
else
    pytest tests/smoke/ -v --cov --cov-report=term-missing --tb=short
fi

echo ""
echo "✅ Smoke tests passed"
echo ""

# Run contract tests (API contracts between services)
echo "2️⃣  Running Contract Tests..."
if $FAST_MODE; then
    pytest tests/contracts/ -v --no-cov --tb=short
else
    pytest tests/contracts/ -v --cov --cov-append --cov-report=term-missing --tb=short
fi

echo ""
echo "✅ Contract tests passed"
echo ""

# Type checking (enabled in Session 42)
echo "3️⃣  Running Type Checks..."
if ! ./scripts/typecheck.sh --quiet; then
    echo ""
    echo "❌ Type checking failed!"
    echo ""
    echo "Run './scripts/typecheck.sh' to see all type errors"
    exit 1
fi

echo ""
echo "✅ Type checking passed"
echo ""

# Summary
echo "=============================================="
echo "✅ All critical tests passed!"
echo ""
echo "These tests validate:"
echo "  ✓ All service modules can be imported"
echo "  ✓ Configuration is valid"
echo "  ✓ Dependencies are working"
echo "  ✓ Service APIs match expectations"
echo "  ✓ Critical HTTP endpoints exist (no 404s)"
echo "  ✓ Services can communicate with each other"
echo "  ✓ Type checking passes (0 errors)"
echo ""
echo "Next steps:"
echo "  • Run unit tests: pytest tests/unit/"
echo "  • Run integration tests: pytest tests/integration/"
echo "  • Run all tests: make test"
echo ""

