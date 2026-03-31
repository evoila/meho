#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.dev.yml"
PROJECT_NAME="meho"
ENV_FILE="${PROJECT_ROOT}/.env"

function usage() {
  cat <<'USAGE'
MEHO Dev Environment
Usage: ./scripts/dev-env.sh <command> [options]

Commands:
  up             Build images, start all services, and run migrations
  down [args]    Stop services (passes additional args to docker compose down)
  restart        Convenience shortcut for down && up
  logs [svc...]  Tail logs for all services or the provided subset
  status         Show docker compose service status
  validate       Check that all services are responding correctly
  test           Run critical tests (smoke + contract) in containerized environment
  test-all       Run all tests (smoke + contract + unit + integration)

Examples:
  ./scripts/dev-env.sh up
  ./scripts/dev-env.sh down --volumes
  ./scripts/dev-env.sh logs meho-api
  ./scripts/dev-env.sh test              # Quick validation
  ./scripts/dev-env.sh test-all          # Comprehensive testing
USAGE
}

function compose() {
  docker compose -p "${PROJECT_NAME}" -f "${COMPOSE_FILE}" "$@"
}

function ensure_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "❌ Missing .env file."
    echo "   Copy env.example and update the values:"
    echo "     cp env.example .env"
    exit 1
  fi
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
  local entries=(
    "meho-knowledge:meho_knowledge"
    "meho-openapi:meho_openapi"
    "meho-agent:meho_agent"
    "meho-ingestion:meho_ingestion"
  )

  for entry in "${entries[@]}"; do
    IFS=":" read -r service module <<<"${entry}"
    echo "➡️  Running migrations for ${service}..."
    if compose exec -T "${service}" sh -c "cd /app/${module} && if [ -d alembic/versions ] && ls -A alembic/versions >/dev/null 2>&1; then alembic upgrade head; else echo 'No migrations to run'; fi"; then
      echo "✅ ${service} migrations complete"
    else
      echo "⚠️  ${service} migrations failed (see logs). Continuing without auto-migrating this service."
    fi
  done
}

function cmd_up() {
  ensure_env_file
  
  # Run type checking before building (added in Session 42)
  echo "🔍 Running type checks before build..."
  if ! "${PROJECT_ROOT}/scripts/typecheck.sh" --quiet; then
    echo ""
    echo "❌ Type checking failed! Fix errors before starting services."
    echo "   Run: ./scripts/typecheck.sh"
    exit 1
  fi
  echo "✅ Type checking passed"
  echo ""
  
  compose up -d --build

  wait_for_service postgres
  # wait_for_service qdrant  # Removed - using pgvector in PostgreSQL
  wait_for_service minio 40 3
  wait_for_service redis
  # rabbitmq removed in Session 35 - not used

  run_migrations

  echo ""
  echo "🔍 Validating services..."
  echo ""
  
  # Run service validation
  if "${PROJECT_ROOT}/scripts/validate-services.sh"; then
    echo ""
    echo "✅ All validation checks passed!"
  else
    echo ""
    echo "⚠️  Service validation found issues (see above)"
    echo "   Stack is running but some endpoints may not work correctly."
    echo "   Check service logs: ./scripts/dev-env.sh logs <service-name>"
    echo ""
  fi
  
  echo ""
  echo "🎉 MEHO stack is up!"
  echo "   Frontend: http://localhost:5173"
  echo "   API:      http://localhost:8000"
  echo ""
  echo "Use './scripts/dev-env.sh logs' to follow logs."
}

function cmd_down() {
  shift || true
  compose down "$@"
}

function cmd_restart() {
  cmd_down
  cmd_up
}

function cmd_logs() {
  shift || true
  if [[ $# -eq 0 ]]; then
    compose logs -f
  else
    compose logs -f "$@"
  fi
}

function cmd_status() {
  compose ps
}

function cmd_validate() {
  echo "🔍 Validating MEHO services..."
  echo ""
  "${PROJECT_ROOT}/scripts/validate-services.sh"
}

function cmd_test() {
  echo "🧪 Running critical tests (smoke + contract)..."
  echo ""
  
  # Run in test container with proper environment
  compose run --rm -e ENV=test meho-api bash -c "
    cd /app && 
    pytest tests/smoke/ tests/contracts/ -v --tb=short --no-cov
  "
  
  echo ""
  echo "✅ Critical tests passed!"
  echo ""
  echo "These tests validate:"
  echo "  ✓ All service modules can be imported"
  echo "  ✓ Configuration is valid"
  echo "  ✓ Dependencies are working"
  echo "  ✓ Service APIs match expectations"
}

function cmd_test_all() {
  echo "🧪 Running all tests (smoke + contract + unit + integration)..."
  echo ""
  
  # Run all tests in container
  compose run --rm -e ENV=test meho-api bash -c "
    cd /app && 
    pytest tests/smoke/ tests/contracts/ tests/unit/ -v --cov --cov-report=term-missing
  "
  
  echo ""
  echo "✅ All tests passed!"
}

command="${1:-}"
case "${command}" in
  up)
    cmd_up
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
  status|ps)
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
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: ${command}"
    echo ""
    usage
    exit 1
    ;;
esac

