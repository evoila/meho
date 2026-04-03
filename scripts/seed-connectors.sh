#!/bin/bash
# =============================================================================
# MEHO Connector Seeding Script
# =============================================================================
# 
# This script creates connectors for your infrastructure after a database reset.
# 
# Usage:
#   ./scripts/seed-connectors.sh              # Create all connectors
#   ./scripts/seed-connectors.sh proxmox      # Create only Proxmox connector
#   ./scripts/seed-connectors.sh gcp          # Create only GCP connector  
#   ./scripts/seed-connectors.sh k8s-proxmox  # Create only K8s on Proxmox connector
#   ./scripts/seed-connectors.sh k8s-gcp      # Create only K8s on GCP connector
#
# Prerequisites:
#   1. Copy .env.connectors.example to .env.connectors
#   2. Fill in your actual credentials in .env.connectors
#   3. Backend must be running (./scripts/dev-env.sh local)
#   4. You must be authenticated (have a valid token)
#
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CREDENTIALS_FILE="${PROJECT_ROOT}/.env.connectors"
API_BASE_URL="${MEHO_API_URL:-http://localhost:8000}"

# =============================================================================
# Helper Functions
# =============================================================================

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
    return 0
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
    return 0
}

log_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
    return 0
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
    return 0
}

log_header() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    return 0
}

check_dependencies() {
    if ! command -v curl &> /dev/null; then
        log_error "curl is required but not installed"
        exit 1
    fi

    if ! command -v jq &> /dev/null; then
        log_warning "jq is not installed. Responses will not be pretty-printed."
    fi
    return 0
}

load_credentials() {
    if [[ ! -f "$CREDENTIALS_FILE" ]]; then
        log_error "Credentials file not found: $CREDENTIALS_FILE"
        log_info "Please copy .env.connectors.example to .env.connectors and fill in your credentials"
        exit 1
    fi

    # shellcheck source=/dev/null
    source "$CREDENTIALS_FILE"
    log_success "Loaded credentials from $CREDENTIALS_FILE"
    return 0
}

check_api_health() {
    log_info "Checking API health..."

    HEALTH_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "${API_BASE_URL}/health" 2>/dev/null || echo "000")

    if [[ "$HEALTH_RESPONSE" != "200" ]]; then
        log_error "API is not responding (status: $HEALTH_RESPONSE)"
        log_info "Make sure the backend is running: ./scripts/dev-env.sh local"
        exit 1
    fi

    log_success "API is healthy"
    return 0
}

get_auth_token() {
    # Check if token is already set
    if [[ -n "$MEHO_AUTH_TOKEN" ]]; then
        log_success "Using provided auth token"
        return
    fi
    
    # Try to login with credentials
    if [[ -n "$MEHO_USERNAME" && -n "$MEHO_PASSWORD" ]]; then
        log_info "Authenticating with MEHO..."
        
        LOGIN_RESPONSE=$(curl -s -X POST "${API_BASE_URL}/api/auth/login" \
            -H "Content-Type: application/json" \
            -d "{\"username\": \"$MEHO_USERNAME\", \"password\": \"$MEHO_PASSWORD\"}")
        
        MEHO_AUTH_TOKEN=$(echo "$LOGIN_RESPONSE" | jq -r '.access_token // empty' 2>/dev/null)
        
        if [[ -z "$MEHO_AUTH_TOKEN" ]]; then
            log_error "Failed to authenticate. Check MEHO_USERNAME and MEHO_PASSWORD"
            log_info "Response: $LOGIN_RESPONSE"
            exit 1
        fi
        
        log_success "Authenticated successfully"
    else
        log_error "No authentication token or credentials provided"
        log_info "Set MEHO_AUTH_TOKEN or MEHO_USERNAME/MEHO_PASSWORD in .env.connectors"
        exit 1
    fi
}

make_api_call() {
    local endpoint="$1"
    local data="$2"
    local description="$3"
    
    log_info "Creating: $description"
    
    RESPONSE=$(curl -s -X POST "${API_BASE_URL}${endpoint}" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${MEHO_AUTH_TOKEN}" \
        -d "$data")
    
    # Check for errors
    ERROR=$(echo "$RESPONSE" | jq -r '.detail // empty' 2>/dev/null)
    
    if [[ -n "$ERROR" ]]; then
        log_error "Failed to create $description"
        log_info "Error: $ERROR"
        return 1
    fi
    
    # Extract connector ID
    CONNECTOR_ID=$(echo "$RESPONSE" | jq -r '.id // empty' 2>/dev/null)
    
    if [[ -n "$CONNECTOR_ID" ]]; then
        log_success "Created $description (ID: $CONNECTOR_ID)"
        if command -v jq &> /dev/null; then
            echo "$RESPONSE" | jq '.'
        else
            echo "$RESPONSE"
        fi
        echo ""
        return 0
    else
        log_warning "Unexpected response for $description"
        echo "$RESPONSE"
        return 1
    fi
}

# =============================================================================
# Connector Creation Functions
# =============================================================================

create_proxmox_connector() {
    log_header "Creating Proxmox Connector (On-Prem Datacenter)"
    
    # Validate required variables
    if [[ -z "$PROXMOX_HOST" ]]; then
        log_error "PROXMOX_HOST is not set in .env.connectors"
        return 1
    fi
    
    # Determine auth method
    local auth_json=""
    if [[ -n "$PROXMOX_API_TOKEN_ID" && -n "$PROXMOX_API_TOKEN_SECRET" ]]; then
        auth_json='"api_token_id": "'"$PROXMOX_API_TOKEN_ID"'", "api_token_secret": "'"$PROXMOX_API_TOKEN_SECRET"'"'
    elif [[ -n "$PROXMOX_USERNAME" && -n "$PROXMOX_PASSWORD" ]]; then
        auth_json='"username": "'"$PROXMOX_USERNAME"'", "password": "'"$PROXMOX_PASSWORD"'"'
    else
        log_error "Proxmox authentication not configured. Set either:"
        log_info "  - PROXMOX_API_TOKEN_ID + PROXMOX_API_TOKEN_SECRET (recommended)"
        log_info "  - PROXMOX_USERNAME + PROXMOX_PASSWORD"
        return 1
    fi
    
    local data='{
        "name": "'"${PROXMOX_NAME:-Proxmox On-Prem}"'",
        "description": "'"${PROXMOX_DESCRIPTION:-On-premise Proxmox VE datacenter}"'",
        "host": "'"$PROXMOX_HOST"'",
        "port": '"${PROXMOX_PORT:-8006}"',
        "disable_ssl_verification": '"${PROXMOX_SKIP_TLS:-false}"',
        '"$auth_json"'
    }'
    
    make_api_call "/api/connectors/proxmox" "$data" "Proxmox connector"
}

create_gcp_connector() {
    log_header "Creating GCP Connector"
    
    # Validate required variables
    if [[ -z "$GCP_PROJECT_ID" ]]; then
        log_error "GCP_PROJECT_ID is not set in .env.connectors"
        return 1
    fi
    
    if [[ -z "$GCP_SERVICE_ACCOUNT_JSON" && -z "$GCP_SERVICE_ACCOUNT_FILE" ]]; then
        log_error "GCP service account not configured. Set either:"
        log_info "  - GCP_SERVICE_ACCOUNT_JSON (JSON content)"
        log_info "  - GCP_SERVICE_ACCOUNT_FILE (path to JSON file)"
        return 1
    fi
    
    # Load service account JSON
    local sa_json=""
    if [[ -n "$GCP_SERVICE_ACCOUNT_FILE" && -f "$GCP_SERVICE_ACCOUNT_FILE" ]]; then
        # Escape JSON for embedding in JSON string
        sa_json=$(cat "$GCP_SERVICE_ACCOUNT_FILE" | jq -c '.' | sed 's/"/\\"/g')
    else
        sa_json=$(echo "$GCP_SERVICE_ACCOUNT_JSON" | sed 's/"/\\"/g')
    fi
    
    local data='{
        "name": "'"${GCP_NAME:-GCP Production}"'",
        "description": "'"${GCP_DESCRIPTION:-Google Cloud Platform connector}"'",
        "project_id": "'"$GCP_PROJECT_ID"'",
        "default_region": "'"${GCP_DEFAULT_REGION:-us-central1}"'",
        "default_zone": "'"${GCP_DEFAULT_ZONE:-us-central1-a}"'",
        "service_account_json": "'"$sa_json"'"
    }'
    
    make_api_call "/api/connectors/gcp" "$data" "GCP connector"
}

create_k8s_proxmox_connector() {
    log_header "Creating Kubernetes Connector (on Proxmox)"
    
    # Validate required variables
    if [[ -z "$K8S_PROXMOX_SERVER_URL" ]]; then
        log_error "K8S_PROXMOX_SERVER_URL is not set in .env.connectors"
        return 1
    fi
    
    if [[ -z "$K8S_PROXMOX_TOKEN" ]]; then
        log_error "K8S_PROXMOX_TOKEN is not set in .env.connectors"
        log_info "To get the token, run: kubectl create token <service-account> -n <namespace>"
        return 1
    fi
    
    local ca_cert_json=""
    if [[ -n "$K8S_PROXMOX_CA_CERT" ]]; then
        ca_cert_json='"ca_certificate": "'"$K8S_PROXMOX_CA_CERT"'",'
    elif [[ -n "$K8S_PROXMOX_CA_CERT_FILE" && -f "$K8S_PROXMOX_CA_CERT_FILE" ]]; then
        local ca_base64=$(cat "$K8S_PROXMOX_CA_CERT_FILE" | base64 | tr -d '\n')
        ca_cert_json='"ca_certificate": "'"$ca_base64"'",'
    fi
    
    local data='{
        "name": "'"${K8S_PROXMOX_NAME:-Kubernetes (Proxmox)}"'",
        "description": "'"${K8S_PROXMOX_DESCRIPTION:-Kubernetes cluster running on Proxmox}"'",
        "server_url": "'"$K8S_PROXMOX_SERVER_URL"'",
        "token": "'"$K8S_PROXMOX_TOKEN"'",
        "skip_tls_verification": '"${K8S_PROXMOX_SKIP_TLS:-false}"',
        '"$ca_cert_json"'
        "_end": true
    }'
    
    # Remove the trailing "_end" field (JSON hack for trailing comma)
    data=$(echo "$data" | sed 's/,"_end": true//g')
    
    make_api_call "/api/connectors/kubernetes" "$data" "Kubernetes (Proxmox) connector"
}

create_k8s_gcp_connector() {
    log_header "Creating Kubernetes Connector (on GCP)"
    
    # Validate required variables
    if [[ -z "$K8S_GCP_SERVER_URL" ]]; then
        log_error "K8S_GCP_SERVER_URL is not set in .env.connectors"
        return 1
    fi
    
    if [[ -z "$K8S_GCP_TOKEN" ]]; then
        log_error "K8S_GCP_TOKEN is not set in .env.connectors"
        log_info "To get the token, run: kubectl create token <service-account> -n <namespace>"
        return 1
    fi
    
    local ca_cert_json=""
    if [[ -n "$K8S_GCP_CA_CERT" ]]; then
        ca_cert_json='"ca_certificate": "'"$K8S_GCP_CA_CERT"'",'
    elif [[ -n "$K8S_GCP_CA_CERT_FILE" && -f "$K8S_GCP_CA_CERT_FILE" ]]; then
        local ca_base64=$(cat "$K8S_GCP_CA_CERT_FILE" | base64 | tr -d '\n')
        ca_cert_json='"ca_certificate": "'"$ca_base64"'",'
    fi
    
    local data='{
        "name": "'"${K8S_GCP_NAME:-Kubernetes (GCP)}"'",
        "description": "'"${K8S_GCP_DESCRIPTION:-GKE Kubernetes cluster on Google Cloud}"'",
        "server_url": "'"$K8S_GCP_SERVER_URL"'",
        "token": "'"$K8S_GCP_TOKEN"'",
        "skip_tls_verification": '"${K8S_GCP_SKIP_TLS:-false}"',
        '"$ca_cert_json"'
        "_end": true
    }'
    
    # Remove the trailing "_end" field (JSON hack for trailing comma)
    data=$(echo "$data" | sed 's/,"_end": true//g')
    
    make_api_call "/api/connectors/kubernetes" "$data" "Kubernetes (GCP) connector"
}

# =============================================================================
# Main
# =============================================================================

main() {
    local connector_type="${1:-all}"
    
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║                    MEHO Connector Seeding Script                     ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    check_dependencies
    load_credentials
    check_api_health
    get_auth_token
    
    local success_count=0
    local error_count=0
    
    case "$connector_type" in
        proxmox)
            if create_proxmox_connector; then ((success_count++)); else ((error_count++)); fi
            ;;
        gcp)
            if create_gcp_connector; then ((success_count++)); else ((error_count++)); fi
            ;;
        k8s-proxmox)
            if create_k8s_proxmox_connector; then ((success_count++)); else ((error_count++)); fi
            ;;
        k8s-gcp)
            if create_k8s_gcp_connector; then ((success_count++)); else ((error_count++)); fi
            ;;
        all)
            # Create all connectors
            if create_proxmox_connector; then ((success_count++)); else ((error_count++)); fi
            if create_gcp_connector; then ((success_count++)); else ((error_count++)); fi
            if create_k8s_proxmox_connector; then ((success_count++)); else ((error_count++)); fi
            if create_k8s_gcp_connector; then ((success_count++)); else ((error_count++)); fi
            ;;
        *)
            log_error "Unknown connector type: $connector_type"
            echo ""
            echo "Usage: $0 [proxmox|gcp|k8s-proxmox|k8s-gcp|all]"
            exit 1
            ;;
    esac
    
    # Summary
    log_header "Summary"
    echo ""
    echo -e "  ${GREEN}✅ Created: $success_count${NC}"
    echo -e "  ${RED}❌ Failed:  $error_count${NC}"
    echo ""
    
    if [[ $error_count -gt 0 ]]; then
        exit 1
    fi
    return 0
}

main "$@"

