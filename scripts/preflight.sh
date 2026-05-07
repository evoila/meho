#!/usr/bin/env bash
# scripts/preflight.sh
#
# Pre-flight check for evaluators: verify host and repo state before running
# `docker compose up`. Prints a colored pass/warn/fail checklist. Exits 0 when
# no blockers are present, 1 otherwise.
#
# Usage: ./scripts/preflight.sh
#
# Companion to scripts/validate-services.sh (post-up health checks) and the
# upcoming maintainer smoke command (task #279). See
# docs/codebase/first-run-experience.md for context.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"

# --- Color setup (respects NO_COLOR and non-TTY stdout) -----------------------

if [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]]; then
  RED=''
  GREEN=''
  YELLOW=''
  BOLD=''
  NC=''
else
  RED=$'\033[0;31m'
  GREEN=$'\033[0;32m'
  YELLOW=$'\033[1;33m'
  BOLD=$'\033[1m'
  NC=$'\033[0m'
fi

# --- Counters and reporting helpers -------------------------------------------

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
NEED_TEI_PORTS=0

pass() {
  printf '%s✓%s %s\n' "$GREEN" "$NC" "$1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn() {
  printf '%s⚠%s %s\n' "$YELLOW" "$NC" "$1"
  if [[ -n "${2:-}" ]]; then
    printf '  → %s\n' "$2"
  fi
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  printf '%s✗%s %s\n' "$RED" "$NC" "$1"
  if [[ -n "${2:-}" ]]; then
    printf '  → %s\n' "$2"
  fi
  if [[ -n "${3:-}" ]]; then
    printf '  → %s\n' "$3"
  fi
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

info() {
  printf '  %s\n' "$1"
}

section() {
  printf '\n%s%s%s\n' "$BOLD" "$1" "$NC"
}

# --- env lookup helper --------------------------------------------------------

# Print the raw value of $1 from .env, or an empty string. Strips surrounding
# quotes; preserves embedded `=` characters; tolerates the file being absent.
env_value() {
  local key=$1
  if [[ ! -f "$ENV_FILE" ]]; then
    return 0
  fi
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null \
    | tail -n1 \
    | cut -d'=' -f2- \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" || true
}

# Return 0 when version $1 is greater-than-or-equal to version $2.
# Both arguments are dotted numeric strings ("26.2" or "26.2.0"). Uses sort -V,
# which is portable across BSD sort (macOS) and GNU sort (Linux).
version_ge() {
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

# Return 0 (true) when $1 looks like an unfilled placeholder — empty, a known
# stub substring, a wrapped token, or shorter than the optional $2 minimum
# length. Covers docs-style placeholders like `sk-ant-your-key-here` that the
# previous prefix-only regex let through. Patterns are lowercase-anchored; real
# provider keys don't contain these substrings.
is_placeholder_value() {
  local value=$1
  local min_len=${2:-0}
  local suspicious='(your-|your_|<[a-z0-9_-]+>|changeme|example-|replace-me|replace_me|placeholder|-here$|_here$|-here-|xxxx|todo-|todo_)'

  if [[ -z "$value" ]]; then
    return 0
  fi
  if [[ "$value" =~ $suspicious ]]; then
    return 0
  fi
  if [[ "$min_len" -gt 0 ]] && [[ ${#value} -lt $min_len ]]; then
    return 0
  fi
  return 1
}

# --- Host checks --------------------------------------------------------------

check_docker_engine() {
  local server_version
  if ! command -v docker >/dev/null 2>&1; then
    fail "Docker engine not detected" \
         "Install Docker Desktop (macOS/Windows) or Docker Engine (Linux)"
    return
  fi
  server_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)
  if [[ -z "$server_version" ]]; then
    fail "Docker daemon not running or unreachable" \
         "Start Docker Desktop (or 'sudo systemctl start docker' on Linux)"
    return
  fi
  if version_ge "$server_version" "24.0"; then
    pass "Docker engine ${server_version} detected"
  else
    fail "Docker engine ${server_version} is below required 24.0" \
         "Upgrade Docker Desktop or Docker Engine to 24.0 or newer"
  fi
}

check_docker_compose() {
  local compose_version
  compose_version=$(docker compose version --short 2>/dev/null || true)
  if [[ -z "$compose_version" ]]; then
    fail "Docker Compose v2 plugin not detected" \
         "Update Docker Desktop, or install the docker-compose-plugin package"
    return
  fi
  if version_ge "$compose_version" "2.20"; then
    pass "Docker Compose ${compose_version} detected"
  else
    fail "Docker Compose ${compose_version} is below required 2.20" \
         "Upgrade Docker Desktop, or update the docker-compose-plugin package"
  fi
}

check_host_arch() {
  local arch kernel
  arch=$(uname -m)
  kernel=$(uname -s)
  case "$arch" in
    arm64|aarch64)
      pass "Host arch: ${arch}"
      if [[ "$kernel" == "Darwin" ]]; then
        warn "Cannot programmatically verify Rosetta 2 is enabled in Docker Desktop" \
             "TEI sidecars are amd64; enable Docker Desktop > Settings > General > 'Use Rosetta for x86_64/amd64 emulation on Apple Silicon'. See docs/troubleshooting.md."
      fi
      ;;
    x86_64|amd64)
      pass "Host arch: ${arch}"
      ;;
    *)
      warn "Host arch: ${arch} (untested)" \
           "MEHO is regularly tested on x86_64 and arm64 only"
      ;;
  esac
}

check_disk_space() {
  local avail_kb avail_gb
  local df_path="$PROJECT_ROOT"
  local df_label="project root"
  local kernel
  kernel=$(uname -s)

  # On Linux, Docker's image/volume storage (`/var/lib/docker` by default) is
  # often on a separate mount from PROJECT_ROOT — `/` can have 500 GB free
  # while `/var` is full. Ask the daemon where its root lives and df against
  # that path instead. On macOS Docker Desktop the reported path is *inside*
  # the Docker VM (e.g. `/var/lib/docker`), not a host path, so we keep the
  # PROJECT_ROOT proxy there — the VM image lives under ~/Library and in
  # practice shares the /Users volume the project root is on.
  if [[ "$kernel" == "Linux" ]] && command -v docker >/dev/null 2>&1; then
    local docker_root
    docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    if [[ -n "$docker_root" ]] && [[ -d "$docker_root" ]]; then
      df_path="$docker_root"
      df_label="Docker root (${docker_root})"
    fi
  fi

  avail_kb=$(df -k "$df_path" 2>/dev/null | tail -n1 | awk '{print $4}' || true)
  if [[ -z "$avail_kb" ]] || ! [[ "$avail_kb" =~ ^[0-9]+$ ]]; then
    warn "Disk space check skipped (df returned unexpected output for ${df_path})"
    return
  fi
  avail_gb=$((avail_kb / 1024 / 1024))
  if [[ "$avail_gb" -ge 10 ]]; then
    pass "Disk space: ${avail_gb} GB available on ${df_label}"
  else
    fail "Disk space: ${avail_gb} GB available on ${df_label} (need 10 GB minimum)" \
         "Free up space; first boot pulls 4-6 GB of images and writes ~2 GB of model weights if using TEI"
  fi
}

check_host_ram() {
  local total_gb=0
  local kernel
  kernel=$(uname -s)
  case "$kernel" in
    Darwin)
      local bytes
      bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
      total_gb=$((bytes / 1024 / 1024 / 1024))
      ;;
    Linux)
      local kb
      kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
      total_gb=$((kb / 1024 / 1024))
      ;;
    *)
      warn "Host RAM check skipped (kernel ${kernel} not supported)"
      return
      ;;
  esac
  if [[ "$total_gb" -ge 8 ]]; then
    pass "Host RAM: ${total_gb} GB"
  else
    warn "Host RAM: ${total_gb} GB (recommended 8 GB)" \
         "MEHO will run, but TEI workers and Keycloak under load may struggle"
  fi
}

# --- Repo state checks --------------------------------------------------------

check_env_file_present() {
  if [[ -f "$ENV_FILE" ]]; then
    pass ".env file exists"
    return 0
  fi
  fail ".env file is missing" \
       "Run: cp env.example .env  (then edit values)"
  return 1
}

check_env_llm_provider() {
  local anthropic openai ollama
  local any_set=0
  anthropic=$(env_value "ANTHROPIC_API_KEY")
  openai=$(env_value "OPENAI_API_KEY")
  ollama=$(env_value "OLLAMA_BASE_URL")

  # Anthropic keys are ~100+ chars (sk-ant-api03-<long>); 40 is a safe floor.
  if ! is_placeholder_value "$anthropic" 40; then
    if [[ "$anthropic" =~ ^sk-ant- ]]; then
      pass "ANTHROPIC_API_KEY set (sk-ant-* prefix)"
    else
      pass "ANTHROPIC_API_KEY set"
    fi
    any_set=1
  fi
  # OpenAI keys are 40+ chars; real keys won't match the suspicious substrings.
  if ! is_placeholder_value "$openai" 40; then
    pass "OPENAI_API_KEY set"
    any_set=1
  fi
  # OLLAMA_BASE_URL is a URL, so no min-length. Still reject stub substrings.
  if ! is_placeholder_value "$ollama" 0; then
    pass "OLLAMA_BASE_URL set (${ollama})"
    any_set=1
  fi

  if [[ "$any_set" -eq 0 ]]; then
    fail "No valid LLM provider key set in .env" \
         "Set at least one of: ANTHROPIC_API_KEY (recommended), OPENAI_API_KEY, OLLAMA_BASE_URL" \
         "Values containing 'your-', 'xxxx', '<...>', 'here' etc. are treated as placeholders."
  fi
}

check_env_stale_models() {
  if [[ ! -f "$ENV_FILE" ]]; then
    return
  fi
  if grep -q -E '^[[:space:]]*[A-Z_]+_MODEL=.*claude-haiku-4-6' "$ENV_FILE" 2>/dev/null; then
    fail "Stale model ID 'claude-haiku-4-6' present in .env" \
         "Replace with 'claude-haiku-4-5' (or remove the override to use compose defaults)"
  else
    pass "No known-stale model IDs in .env"
  fi
}

check_env_encryption_key() {
  local key env_mode
  local placeholder='your-secret-key-at-least-32-characters-long'
  # Fernet keys are 32 random bytes URL-safe-base64-encoded → exactly 44 chars
  # ending in '=' with the URL-safe alphabet (A-Z, a-z, 0-9, _, -).
  local fernet_re='^[A-Za-z0-9_-]{43}=$'
  # Stdlib-only remediation. `openssl rand -base64 32` alone is not valid for
  # Fernet (standard base64 uses +/ but Fernet requires URL-safe -_).
  local gen_hint="Generate a key: openssl rand -base64 32 | tr '+/' '_-'  (or: python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  key=$(env_value "CREDENTIAL_ENCRYPTION_KEY")
  env_mode=$(env_value "APP_ENVIRONMENT")
  env_mode="${env_mode:-dev}"

  if [[ -z "$key" ]]; then
    fail "CREDENTIAL_ENCRYPTION_KEY is empty in .env" "$gen_hint"
    return
  fi
  if [[ "$key" == "$placeholder" ]]; then
    if [[ "$env_mode" == "prod" ]]; then
      fail "CREDENTIAL_ENCRYPTION_KEY is the literal placeholder (APP_ENVIRONMENT=prod)" \
           "$gen_hint"
    else
      warn "CREDENTIAL_ENCRYPTION_KEY is the literal placeholder (OK for local eval)" \
           "Backend will reject this when encrypting connector creds. $gen_hint"
    fi
    return
  fi
  if ! [[ "$key" =~ $fernet_re ]]; then
    # A non-placeholder, non-Fernet-shaped value is a definite runtime failure
    # (cryptography.fernet.Fernet() will raise ValueError at backend startup).
    # Blocker regardless of APP_ENVIRONMENT.
    fail "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key" \
         "Expected 44-char URL-safe base64 (32 random bytes). Backend will raise ValueError at startup." \
         "$gen_hint"
    return
  fi
  pass "CREDENTIAL_ENCRYPTION_KEY has a valid Fernet shape"
}

check_env_provider_path() {
  info "Embeddings path: local TEI sidecars (bge-m3 + bge-reranker-v2-m3)"
  NEED_TEI_PORTS=1
}

# --- Port checks --------------------------------------------------------------

# Returns 0 if a TCP listener is already bound to localhost:$1. Uses bash's
# /dev/tcp/ builtin so no external tool (lsof, nc) is required. Subshell +
# stderr suppression because connection refusal is the expected case under
# `set -e`.
port_in_use() {
  local port=$1
  (echo > "/dev/tcp/127.0.0.1/${port}") 2>/dev/null
}

check_port() {
  local port=$1 name=$2
  if port_in_use "$port"; then
    fail "Port ${port} (${name}) is in use" \
         "Run: docker compose down  (clears prior MEHO containers)" \
         "Or: lsof -nP -iTCP:${port} -sTCP:LISTEN  (identify other process)"
  else
    pass "Port ${port} (${name}) available"
  fi
}

check_ports() {
  check_port 5432 "postgres"
  check_port 6379 "redis"
  check_port 8000 "backend"
  check_port 8080 "keycloak"
  check_port 9000 "minio"
  check_port 9001 "minio console"
  check_port 5341 "seq"
  check_port 5173 "frontend"

  if [[ "$NEED_TEI_PORTS" -eq 1 ]]; then
    check_port 8090 "tei-embeddings"
    check_port 8091 "tei-reranker"
  fi
}

# --- main ---------------------------------------------------------------------

print_header() {
  printf '%sMEHO preflight%s\n' "$BOLD" "$NC"
  printf '%s\n' '──────────────'
}

print_summary() {
  local warn_word="warnings"
  local fail_word="blockers"
  [[ "$WARN_COUNT" -eq 1 ]] && warn_word="warning"
  [[ "$FAIL_COUNT" -eq 1 ]] && fail_word="blocker"

  printf '\n'
  if [[ "$FAIL_COUNT" -eq 0 ]]; then
    printf '%s✓ Preflight OK%s — ready to run %sdocker compose up%s' \
      "$GREEN" "$NC" "$BOLD" "$NC"
    if [[ "$WARN_COUNT" -gt 0 ]]; then
      printf ' (%d %s)' "$WARN_COUNT" "$warn_word"
    fi
    printf '\n'
    return 0
  fi
  printf '%s✗ Preflight FAILED%s — %d %s, %d %s. Fix and rerun.\n' \
    "$RED" "$NC" \
    "$FAIL_COUNT" "$fail_word" \
    "$WARN_COUNT" "$warn_word"
  return 1
}

main() {
  print_header

  section "Host environment"
  check_docker_engine
  check_docker_compose
  check_host_arch
  check_disk_space
  check_host_ram

  section "Repository state"
  if check_env_file_present; then
    check_env_llm_provider
    check_env_stale_models
    check_env_encryption_key
    check_env_provider_path
  fi

  section "Port availability"
  check_ports

  print_summary
}

main "$@"
