#!/bin/bash
#
# Run end-to-end tests for MEHO API
#
# This script:
# 1. Starts all required services (docker compose)
# 2. Waits for services to be ready
# 3. Runs database migrations
# 4. Starts MEHO API
# 5. Runs E2E tests
# 6. Tears down services (optional)
#
# Usage:
#   ./scripts/run-e2e-tests.sh              # Run all E2E tests
#   ./scripts/run-e2e-tests.sh --keep       # Keep services running after tests
#   ./scripts/run-e2e-tests.sh --failures   # Run only failure scenario tests
#

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
KEEP_SERVICES=false
TEST_FILTER=""
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --keep)
            KEEP_SERVICES=true
            shift
            ;;
        --failures)
            TEST_FILTER="-m failure"
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --keep       Keep services running after tests"
            echo "  --failures   Run only failure scenario tests"
            echo "  --help       Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

cd "$PROJECT_ROOT"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}MEHO API End-to-End Test Runner${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

if ! docker compose version &> /dev/null; then
    echo -e "${RED}Error: docker compose not found${NC}"
    exit 1
fi

if ! command -v pytest &> /dev/null; then
    echo -e "${RED}Error: pytest not found. Install with: pip install pytest${NC}"
    exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo -e "${YELLOW}Warning: ANTHROPIC_API_KEY not set. Some tests will fail.${NC}"
    echo -e "${YELLOW}Set it with: export ANTHROPIC_API_KEY=sk-ant-...${NC}"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo -e "${GREEN}✓ Prerequisites OK${NC}"
echo ""

# Step 1: Start services
echo -e "${YELLOW}Step 1: Starting services...${NC}"
docker compose -f docker compose.test.yml up -d

echo -e "${GREEN}✓ Services started${NC}"
echo ""

# Step 2: Wait for services to be ready
echo -e "${YELLOW}Step 2: Waiting for services to be ready...${NC}"

wait_for_service() {
    local service=$1
    local url=$2
    local max_attempts=30
    local attempt=1
    
    echo -n "Waiting for $service"
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s "$url" > /dev/null 2>&1; then
            echo -e " ${GREEN}✓${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
        ((attempt++))
    done
    
    echo -e " ${RED}✗${NC}"
    echo -e "${RED}Error: $service did not start${NC}"
    return 1
}

# Wait for PostgreSQL
echo -n "Waiting for PostgreSQL"
for i in {1..30}; do
    if docker compose -f docker compose.test.yml exec -T postgres pg_isready -U meho > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
    sleep 1
done

echo -e "${GREEN}✓ All services ready${NC}"
echo ""

# Step 3: Run migrations
echo -e "${YELLOW}Step 3: Running database migrations...${NC}"
./scripts/migrate-all.sh || {
    echo -e "${RED}Error: Migration failed${NC}"
    if [ "$KEEP_SERVICES" = false ]; then
        docker compose -f docker compose.test.yml down
    fi
    exit 1
}
echo -e "${GREEN}✓ Migrations complete${NC}"
echo ""

# Step 4: Start MEHO API in background
echo -e "${YELLOW}Step 4: Starting MEHO API...${NC}"

# Set test environment
export ENVIRONMENT=test
export DATABASE_URL="postgresql://meho:meho@localhost:5432/meho_test"
export JWT_SECRET_KEY="test-secret-key-for-e2e-tests"

# Start API in background
uvicorn meho_app.main:app --host 0.0.0.0 --port 8000 > /tmp/meho_api.log 2>&1 &
API_PID=$!

echo "MEHO API started (PID: $API_PID)"

# Wait for API to be ready
wait_for_service "MEHO API" "http://localhost:8000/health" || {
    echo -e "${RED}Error: MEHO API did not start${NC}"
    echo "Last 20 lines of log:"
    tail -20 /tmp/meho_api.log
    kill $API_PID 2>/dev/null || true
    if [ "$KEEP_SERVICES" = false ]; then
        docker compose -f docker compose.test.yml down
    fi
    exit 1
}

echo -e "${GREEN}✓ MEHO API ready${NC}"
echo ""

# Step 5: Run E2E tests
echo -e "${YELLOW}Step 5: Running E2E tests...${NC}"
echo ""

# Install test dependencies
pip install -q httpx-sse pytest-asyncio 2>/dev/null || true

# Run tests
TEST_EXIT_CODE=0
if pytest tests/e2e/test_meho_api_real_services.py \
         tests/e2e/test_meho_api_failure_scenarios.py \
         -v \
         -m e2e \
         $TEST_FILTER \
         --tb=short \
         --color=yes; then
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ All E2E tests passed!${NC}"
    echo -e "${GREEN}========================================${NC}"
else
    TEST_EXIT_CODE=$?
    echo ""
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}✗ Some E2E tests failed${NC}"
    echo -e "${RED}========================================${NC}"
fi

echo ""

# Step 6: Cleanup
if [ "$KEEP_SERVICES" = false ]; then
    echo -e "${YELLOW}Cleaning up services...${NC}"
    
    # Stop API
    kill $API_PID 2>/dev/null || true
    
    # Stop docker services
    docker compose -f docker compose.test.yml down
    
    echo -e "${GREEN}✓ Cleanup complete${NC}"
else
    echo -e "${YELLOW}Services kept running (use --keep flag)${NC}"
    echo ""
    echo "To stop services manually:"
    echo "  docker compose -f docker compose.test.yml down"
    echo ""
    echo "To view API logs:"
    echo "  tail -f /tmp/meho_api.log"
    echo ""
    echo "API PID: $API_PID"
fi

echo ""
exit $TEST_EXIT_CODE

