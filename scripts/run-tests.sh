#!/bin/bash
set -e

echo "========================================="
echo "MEHO Test Suite"
echo "========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if test environment is running
if ! docker ps | grep -q "meho-postgres-test"; then
    echo -e "${YELLOW}Test environment not running. Starting...${NC}"
    ./scripts/test-env-up.sh
fi

echo -e "${GREEN}Running tests...${NC}"
pytest "$@"

TEST_EXIT_CODE=$?

if [[ $TEST_EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
else
    echo -e "${RED}✗ Tests failed${NC}"
fi

exit $TEST_EXIT_CODE

