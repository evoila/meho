#!/usr/bin/env bash
# MEHO Development Environment Script
# Uses docker-compose.yml (community edition default)
# TEI sidecars auto-start only when VOYAGE_API_KEY is not set in .env

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"
PROJECT_NAME="meho"
ENV_FILE="${PROJECT_ROOT}/.env"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

function usage() {
  cat <<'USAGE'
MEHO Development Environment
Usage: ./scripts/dev-env.sh <command> [options]

Commands:
  up             Build images, start all services in Docker, run migrations
  local          Start infrastructure in Docker, run app locally with hot-reload ⚡
  down [args]    Stop services (passes additional args to docker compose down)
  restart        Convenience shortcut for down && up
  logs [svc...]  Tail logs for all services or the provided subset
  status         Show docker compose service status
  validate       Check that all services are responding correctly
  test           Run critical tests (smoke + contract) in containerized environment
  test-all       Run all tests (smoke + contract + unit + integration)

Examples:
  ./scripts/dev-env.sh up                # Full Docker (CI/testing)
  ./scripts/dev-env.sh local             # Hot-reload development ⚡
  ./scripts/dev-env.sh down --volumes
  ./scripts/dev-env.sh logs meho
  ./scripts/dev-env.sh test
USAGE
  return 0
}

function needs_tei_profile() {
  # TEI sidecars are only needed when VOYAGE_API_KEY is not set
  local voyage_key=""
  if [[ -f "${ENV_FILE}" ]]; then
    voyage_key=$(grep -E '^VOYAGE_API_KEY=' "${ENV_FILE}" 2>/dev/null | cut -d'=' -f2- || true)
  fi
  voyage_key="${VOYAGE_API_KEY:-$voyage_key}"
  [[ -z "${voyage_key}" ]]
}

function compose() {
  if needs_tei_profile; then
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" --profile tei "$@"
  else
    docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"
  fi
  return $?
}

function ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "❌ Missing .env file."
    echo "   Copy env.example and update the values:"
    echo "     cp env.example .env"
    exit 1
  fi
  return 0
}

function wait_for_service() {
  local service=$1
  local retries=${2:-60}
  local sleep_seconds=${3:-2}

  echo "⏳ Waiting for ${service} to become healthy..."
  for _ in $(seq 1 "${retries}"); do
    local container_id
    container_id=$(compose ps -q "${service}" 2>/dev/null || true)
    if [[ -z "${container_id}" ]]; then
      sleep "${sleep_seconds}"
      continue
    fi

    local health
    health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")

    if [[ "${health}" == "healthy" ]] || [[ "${health}" == "running" ]]; then
      echo "✅ ${service} is ${health}"
      return 0
    fi

    sleep "${sleep_seconds}"
  done

  echo "❌ ${service} failed to become healthy in time"
  exit 1
}

function run_migrations() {
  echo ""
  echo "🔄 Running database migrations..."
  echo ""

  # For monolith, run migrations inside the unified "meho" service
  if compose exec -T meho sh -c "cd /app && bash scripts/run-migrations-monolith.sh"; then
    echo "✅ All migrations complete"
  else
    echo "⚠️  Migration failed (see logs above)"
    exit 1
  fi
  return 0
}

function run_keycloak_setup() {
  echo ""
  echo "🔐 Configuring Keycloak..."
  if "${PROJECT_ROOT}/scripts/setup-keycloak.sh"; then
    echo ""
  else
    echo "⚠️  Keycloak setup had warnings (see above)"
  fi
  return 0
}

function cmd_up() {
  ensure_env_file
  
  # Run type checking before building
  echo "🔍 Running type checks before build..."
  if ! "${PROJECT_ROOT}/scripts/typecheck.sh" --quiet; then
    echo "⚠️  Type errors detected"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      echo "❌ Aborted. Fix type errors and try again"
      exit 1
    fi
  fi
  echo "✅ Type checking passed"
  echo ""
  
  echo "📦 Building and starting services..."
  compose up -d --build
  
  echo ""
  echo "⏳ Waiting for infrastructure services..."
  wait_for_service postgres
  wait_for_service redis
  wait_for_service keycloak 90 2  # Keycloak takes longer to start
  
  echo ""
  echo "⏳ Waiting for MEHO backend..."
  wait_for_service meho
  
  # Run migrations
  run_migrations
  
  # Configure Keycloak (create clients, users, seed tenant data)
  run_keycloak_setup
  
  echo ""
  echo "========================================"
  echo "✅ MEHO is running!"
  echo "========================================"
  echo ""
  echo "Services:"
  echo "  • Backend API:      http://localhost:8000"
  echo "  • API Docs:         http://localhost:8000/docs"
  echo "  • Frontend:         http://localhost:5173 (if started)"
  echo "  • Keycloak Admin:   http://localhost:8080 (admin/${KEYCLOAK_ADMIN_PASSWORD:-admin})"
  echo "  • MinIO Console:    http://localhost:9001 (admin/minioadmin)"
  echo "  • Seq (Logs):       http://localhost:5341"
  echo "  • PostgreSQL:       localhost:5432 (meho/password)"
  echo "  • Redis:            localhost:6379"
  echo ""
  echo "Commands:"
  echo "  ./scripts/dev-env.sh logs       # View logs"
  echo "  ./scripts/dev-env.sh down       # Stop services"
  echo "  ./scripts/dev-env.sh test       # Run critical tests"
  echo ""
  return 0
}

function cmd_down() {
  echo "🛑 Stopping services..."

  # Kill locally running processes from "local" mode
  # These run outside Docker and need explicit cleanup
  # IMPORTANT: Only kill by process name, NOT by port (to avoid killing Docker)
  echo "  → Stopping local backend (uvicorn)..."
  pkill -f "uvicorn meho_app.main:app" 2>/dev/null || true

  echo "  → Stopping local frontend (vite)..."
  pkill -f "vite.*meho_frontend" 2>/dev/null || true
  pkill -f "node.*meho_frontend" 2>/dev/null || true

  # Give processes time to terminate gracefully
  sleep 1

  # Stop Docker containers
  echo "  → Stopping Docker containers..."
  compose down "$@"

  echo "✅ Services stopped"
  return 0
}

function cmd_restart() {
  cmd_down
  echo ""
  cmd_up
  return 0
}

function cmd_logs() {
  compose logs -f "$@"
  return $?
}

function cmd_status() {
  compose ps
  return $?
}

function cmd_validate() {
  echo "🔍 Validating services..."
  "${PROJECT_ROOT}/scripts/validate-services.sh"
  return $?
}

function cmd_test() {
  echo "🧪 Running critical tests (smoke + contract)..."
  compose exec -T meho bash -c "cd /app && ./scripts/run-critical-tests.sh --fast"
  return $?
}

function cmd_test_all() {
  echo "🧪 Running all tests..."
  compose exec -T meho bash -c "cd /app && pytest tests/"
  return $?
}

# =============================================================================
# Local Development Mode (Hot-Reload)
# =============================================================================

# Virtual environment path
VENV_DIR="${PROJECT_ROOT}/.venv"

function activate_venv() {
  if [[ -d "${VENV_DIR}" ]]; then
    echo -e "${YELLOW}🐍 Activating virtual environment...${NC}"
    source "${VENV_DIR}/bin/activate"
  else
    echo -e "${RED}❌ Virtual environment not found at ${VENV_DIR}${NC}"
    echo "   Create one with: python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
    exit 1
  fi
  return 0
}

function check_local_prerequisites() {
  local missing=()

  # Check Node.js
  if ! command -v node &> /dev/null; then
    missing+=("node")
  fi

  # Check npm
  if ! command -v npm &> /dev/null; then
    missing+=("npm")
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo -e "${RED}❌ Missing prerequisites: ${missing[*]}${NC}"
    echo "   Please install them before running local development."
    exit 1
  fi
  return 0
}

function check_python_deps() {
  # Check if uvicorn is available (key dependency)
  if ! python -c "import uvicorn" &> /dev/null 2>&1; then
    echo -e "${YELLOW}📦 Installing Python dependencies...${NC}"
    cd "${PROJECT_ROOT}"
    pip install -e ".[dev]" --quiet
  fi
  return 0
}

function install_mcp_server() {
  # Install the MEHO MCP server for Cursor introspection
  if ! python -c "import meho_mcp_server" &> /dev/null 2>&1; then
    echo -e "${YELLOW}📦 Installing MEHO MCP server for Cursor introspection...${NC}"
    cd "${PROJECT_ROOT}"
    pip install -e "./meho_mcp_server" --quiet
    echo -e "${GREEN}✅ MCP server installed (meho-mcp-server)${NC}"
  fi
  return 0
}

function check_frontend_deps() {
  if [[ ! -d "${PROJECT_ROOT}/meho_frontend/node_modules" ]]; then
    echo -e "${YELLOW}📦 Installing frontend dependencies...${NC}"
    cd "${PROJECT_ROOT}/meho_frontend"
    npm install
  fi
  return 0
}

function run_migrations_local() {
  echo ""
  echo -e "${BLUE}🔄 Running database migrations...${NC}"

  cd "${PROJECT_ROOT}"

  # Source .env file for local environment
  set -a
  source "${ENV_FILE}"
  set +a

  # Override DATABASE_URL for local connection (localhost instead of postgres container)
  export DATABASE_URL="postgresql+asyncpg://meho:password@localhost:5432/meho"

  # Run migrations for each module
  # NOTE: Order matters! topology must run before connectors (foreign key dependency)
  local modules=("knowledge" "topology" "connectors" "memory" "agents" "ingestion" "scheduled_tasks" "orchestrator_skills" "audit")

  for module in "${modules[@]}"; do
    local alembic_ini="${PROJECT_ROOT}/meho_app/modules/${module}/alembic.ini"
    if [[ -f "${alembic_ini}" ]]; then
      echo "  → Migrating ${module}..."
      alembic -c "${alembic_ini}" upgrade head 2>/dev/null || true
    fi
  done

  echo -e "${GREEN}✅ Migrations complete${NC}"
  return 0
}

function cleanup_local_processes() {
  # Kill any existing local processes from previous runs
  # IMPORTANT: Only kill by process name to avoid accidentally killing Docker daemon
  local cleaned=false

  # Check for and kill backend process
  if pgrep -f "uvicorn meho_app.main:app" &>/dev/null; then
    echo -e "${YELLOW}  → Stopping existing backend process...${NC}"
    pkill -f "uvicorn meho_app.main:app" 2>/dev/null || true
    cleaned=true
  fi

  # Check for and kill frontend process
  if pgrep -f "vite.*meho_frontend" &>/dev/null || pgrep -f "node.*meho_frontend" &>/dev/null; then
    echo -e "${YELLOW}  → Stopping existing frontend process...${NC}"
    pkill -f "vite.*meho_frontend" 2>/dev/null || true
    pkill -f "node.*meho_frontend" 2>/dev/null || true
    cleaned=true
  fi

  if $cleaned; then
    sleep 2  # Give processes time to terminate gracefully
  fi
  return 0
}

function cmd_local() {
  ensure_env_file
  activate_venv
  check_local_prerequisites
  
  echo ""
  echo -e "${BLUE}🚀 MEHO Local Development Mode${NC}"
  echo -e "${BLUE}================================${NC}"
  echo ""
  echo -e "${YELLOW}This mode runs:${NC}"
  echo "  • Infrastructure (postgres, redis, minio, keycloak) in Docker"
  echo "  • Backend locally with hot-reload (uvicorn --reload)"
  echo "  • Frontend locally with hot-reload (vite dev)"
  echo ""
  
  # Clean up any existing local processes
  cleanup_local_processes
  
  # Stop the Docker-based meho and frontend services if running
  # (they would conflict with local hot-reload services)
  echo -e "${YELLOW}📦 Ensuring Docker app services are stopped...${NC}"
  compose stop meho meho-frontend 2>/dev/null || true
  compose rm -f meho meho-frontend 2>/dev/null || true
  
  # Start infrastructure only (including Seq for log visualization)
  echo -e "${YELLOW}📦 Starting infrastructure services...${NC}"
  compose up -d postgres redis minio keycloak seq
  
  echo ""
  wait_for_service postgres
  wait_for_service redis
  wait_for_service keycloak 90 2  # Keycloak takes longer to start
  
  # Check and install dependencies
  check_python_deps
  install_mcp_server
  check_frontend_deps
  
  # Run migrations
  run_migrations_local
  
  # Configure Keycloak (create clients, users, seed tenant data)
  run_keycloak_setup
  
  # Source environment variables
  set -a
  source "${ENV_FILE}"
  set +a
  
  # Override for local development (connect to Docker infrastructure)
  export DATABASE_URL="postgresql+asyncpg://meho:password@localhost:5432/meho"
  export REDIS_URL="redis://localhost:6379/0"
  export OBJECT_STORAGE_ENDPOINT="http://localhost:9000"
  export OBJECT_STORAGE_ACCESS_KEY="${OBJECT_STORAGE_ACCESS_KEY:-minioadmin}"
  export OBJECT_STORAGE_SECRET_KEY="${OBJECT_STORAGE_SECRET_KEY:-minioadmin}"
  export OBJECT_STORAGE_BUCKET="${OBJECT_STORAGE_BUCKET:-meho-dev-data}"
  export OBJECT_STORAGE_USE_SSL="${OBJECT_STORAGE_USE_SSL:-false}"
  export ENV="${ENV:-dev}"
  export KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
  export KEYCLOAK_CLIENT_ID="${KEYCLOAK_CLIENT_ID:-meho-api}"
  
  echo ""
  echo -e "${GREEN}========================================"
  echo -e "✅ Infrastructure ready!"
  echo -e "========================================${NC}"
  echo ""
  echo "  Keycloak:  http://localhost:8080 (admin/${KEYCLOAK_ADMIN_PASSWORD:-admin})"
  echo "  Seq (Logs): http://localhost:5341"
  echo ""
  
  # Export OTEL env vars for local development (point to Docker Seq)
  export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-meho}"
  export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:5341/ingest/otlp}"
  export OTEL_CONSOLE="${OTEL_CONSOLE:-true}"
  export MEHO_LOG_LEVEL="${MEHO_LOG_LEVEL:-INFO}"
  
  # Track PIDs for cleanup
  BACKEND_PID=""
  FRONTEND_PID=""
  
  # Cleanup function for graceful shutdown
  cleanup_on_exit() {
    echo ""
    echo -e "${YELLOW}Stopping services...${NC}"
    
    # Kill backend if running
    if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "  → Stopping backend..."
      kill "$BACKEND_PID" 2>/dev/null || true
    fi
    
    # Kill frontend if running
    if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
      echo "  → Stopping frontend..."
      kill "$FRONTEND_PID" 2>/dev/null || true
    fi
    
    # Also clean up by process name (in case PIDs changed)
    pkill -f "uvicorn meho_app.main:app" 2>/dev/null || true
    pkill -f "vite.*meho_frontend" 2>/dev/null || true
    pkill -f "node.*meho_frontend" 2>/dev/null || true
    
    echo -e "${GREEN}✅ Stopped${NC}"
    exit 0
  }
  
  # Set up trap for clean exit
  trap cleanup_on_exit INT TERM EXIT
  
  # Start backend in background
  cd "${PROJECT_ROOT}"
  echo -e "${BLUE}Starting backend...${NC}"
  python -m uvicorn meho_app.main:app --reload --host 0.0.0.0 --port 8000 &
  BACKEND_PID=$!
  
  # Give backend a moment to start
  sleep 3
  
  # Check if backend started successfully
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo -e "${RED}❌ Backend failed to start${NC}"
    exit 1
  fi
  echo -e "${GREEN}✅ Backend running (PID: $BACKEND_PID)${NC}"
  
  # Start frontend in background
  cd "${PROJECT_ROOT}/meho_frontend"
  echo -e "${BLUE}Starting frontend...${NC}"
  npm run dev &
  FRONTEND_PID=$!
  
  sleep 2
  
  # Check if frontend started
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    echo -e "${RED}❌ Frontend failed to start${NC}"
    exit 1
  fi
  echo -e "${GREEN}✅ Frontend running (PID: $FRONTEND_PID)${NC}"
  
  echo ""
  echo -e "${GREEN}========================================"
  echo -e "🎉 Hot-reload development ready!"
  echo -e "========================================${NC}"
  echo ""
  echo "  Backend:   http://localhost:8000"
  echo "  Frontend:  http://localhost:5173"
  echo "  API Docs:  http://localhost:8000/docs"
  echo "  Seq (Logs): http://localhost:5341"
  echo ""
  echo -e "${BLUE}MCP Server:${NC}"
  echo "  The MEHO introspection MCP server is installed."
  echo "  Restart Cursor to enable introspection tools."
  echo "  Tools: meho_list_sessions, meho_get_transcript, etc."
  echo ""
  echo -e "${YELLOW}Press Ctrl+C to stop${NC}"
  echo ""
  
  # Wait for either process to exit
  wait $BACKEND_PID $FRONTEND_PID
  return 0
}

# Parse command
cmd="${1:-}"
shift || true

case "${cmd}" in
  up)
    cmd_up
    ;;
  local)
    cmd_local
    ;;
  down)
    cmd_down "$@"
    ;;
  restart)
    cmd_restart
    ;;
  logs)
    cmd_logs "$@"
    ;;
  status)
    cmd_status
    ;;
  validate)
    cmd_validate
    ;;
  test)
    cmd_test
    ;;
  test-all)
    cmd_test_all
    ;;
  ""| -h | --help)
    usage
    ;;
  *)
    echo "❌ Unknown command: ${cmd}"
    echo ""
    usage
    exit 1
    ;;
esac

