#!/bin/bash
# =============================================================================
# Keycloak Setup Script
# =============================================================================
# This script configures Keycloak for MEHO multi-tenant authentication:
# 1. Waits for Keycloak to be healthy
# 2. Configures the master realm with meho-frontend client (for global admin)
# 3. Seeds tenant configuration in the database
#
# Usage: ./scripts/setup-keycloak.sh
#
# Environment variables:
#   KEYCLOAK_URL      - Keycloak server URL (default: http://localhost:8080)
#   KEYCLOAK_ADMIN    - Admin username (default: admin)
#   KEYCLOAK_ADMIN_PASSWORD - Admin password (default: admin)
#   DATABASE_URL      - PostgreSQL connection string
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
KEYCLOAK_ADMIN="${KEYCLOAK_ADMIN:-admin}"
KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-meho}"
DB_USER="${DB_USER:-meho}"
DB_PASSWORD="${DB_PASSWORD:-password}"

echo -e "${BLUE}==========================================${NC}"
echo -e "${BLUE}   MEHO Keycloak Setup Script${NC}"
echo -e "${BLUE}==========================================${NC}"
echo ""

# =============================================================================
# Helper Functions
# =============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
    return 0
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
    return 0
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
    return 0
}

# Wait for Keycloak to be ready
wait_for_keycloak() {
    log_info "Waiting for Keycloak to be ready at ${KEYCLOAK_URL}..."
    
    local max_attempts=60
    local attempt=1
    
    while [[ $attempt -le $max_attempts ]]; do
        if curl -sf "${KEYCLOAK_URL}/health/ready" > /dev/null 2>&1; then
            log_info "Keycloak is ready!"
            return 0
        fi
        
        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done
    
    log_error "Keycloak did not become ready within timeout"
    return 1
}

# Get admin access token
get_admin_token() {
    local token
    token=$(curl -sf -X POST "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${KEYCLOAK_ADMIN}" \
        -d "password=${KEYCLOAK_ADMIN_PASSWORD}" \
        -d "grant_type=password" \
        -d "client_id=admin-cli" | jq -r '.access_token')
    
    if [[ -z "$token" ]] || [[ "$token" = "null" ]]; then
        log_error "Failed to get admin access token"
        return 1
    fi
    
    echo "$token"
}

# Check if client exists in realm
client_exists() {
    local token=$1
    local realm=$2
    local client_id=$3
    
    local result
    result=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/${realm}/clients" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" | jq -r ".[] | select(.clientId == \"${client_id}\") | .clientId")
    
    [[ "$result" = "$client_id" ]]
}

# Create client in realm
create_client() {
    local token=$1
    local realm=$2
    local client_json=$3
    
    curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/${realm}/clients" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "${client_json}" > /dev/null
    return $?
}

# Disable SSL requirement for a realm (development only)
disable_ssl_for_realm() {
    local token=$1
    local realm=$2
    
    curl -sf -X PUT "${KEYCLOAK_URL}/admin/realms/${realm}" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "{\"sslRequired\": \"none\"}" > /dev/null 2>&1
    return 0
}

# Add meho-profile client scope to a realm (ensures username/email in tokens)
add_profile_scope_to_realm() {
    local token=$1
    local realm=$2
    
    # Check if scope already exists
    local scope_exists
    scope_exists=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/${realm}/client-scopes" \
        -H "Authorization: Bearer ${token}" | jq -r '.[] | select(.name == "meho-profile") | .name // empty')
    
    if [[ -n "$scope_exists" ]]; then
        return 0  # Already exists
    fi
    
    # Create the meho-profile client scope
    local scope_json='{
        "name": "meho-profile",
        "description": "MEHO profile claims in access token",
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true"
        },
        "protocolMappers": [
            {
                "name": "preferred_username",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-property-mapper",
                "consentRequired": false,
                "config": {
                    "user.attribute": "username",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "claim.name": "preferred_username",
                    "jsonType.label": "String"
                }
            },
            {
                "name": "full name",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-full-name-mapper",
                "consentRequired": false,
                "config": {
                    "id.token.claim": "true",
                    "access.token.claim": "true"
                }
            },
            {
                "name": "email",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-property-mapper",
                "consentRequired": false,
                "config": {
                    "user.attribute": "email",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "claim.name": "email",
                    "jsonType.label": "String"
                }
            }
        ]
    }'
    
    # Create the scope
    curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/${realm}/client-scopes" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "${scope_json}" > /dev/null 2>&1
    
    # Get the scope ID
    local scope_id
    scope_id=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/${realm}/client-scopes" \
        -H "Authorization: Bearer ${token}" | jq -r '.[] | select(.name == "meho-profile") | .id')
    
    if [[ -n "$scope_id" ]]; then
        # Add as default scope for meho-frontend client
        local client_id
        client_id=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/${realm}/clients" \
            -H "Authorization: Bearer ${token}" | jq -r '.[] | select(.clientId == "meho-frontend") | .id')
        
        if [[ -n "$client_id" ]]; then
            curl -sf -X PUT "${KEYCLOAK_URL}/admin/realms/${realm}/clients/${client_id}/default-client-scopes/${scope_id}" \
                -H "Authorization: Bearer ${token}" > /dev/null 2>&1
        fi
    fi
    return 0
}

# =============================================================================
# Main Setup Steps
# =============================================================================

# Step 1: Wait for Keycloak
wait_for_keycloak || exit 1

echo ""
log_info "Getting admin access token..."
ADMIN_TOKEN=$(get_admin_token) || exit 1
log_info "Admin token obtained"

# Step 2: Disable SSL for development (all realms)
echo ""
log_info "Disabling SSL requirement for development..."
disable_ssl_for_realm "$ADMIN_TOKEN" "master"
log_info "SSL disabled for master realm"

# Step 3: Configure master realm with meho-frontend client
echo ""
log_info "Configuring master realm..."

MEHO_FRONTEND_CLIENT='{
    "clientId": "meho-frontend",
    "name": "MEHO Frontend Application",
    "description": "Frontend SPA client for OIDC authentication (Global Admin)",
    "enabled": true,
    "publicClient": true,
    "protocol": "openid-connect",
    "standardFlowEnabled": true,
    "implicitFlowEnabled": false,
    "directAccessGrantsEnabled": true,
    "serviceAccountsEnabled": false,
    "authorizationServicesEnabled": false,
    "fullScopeAllowed": true,
    "rootUrl": "http://localhost:5173",
    "baseUrl": "/",
    "redirectUris": [
        "http://localhost:5173/*",
        "http://localhost:3000/*"
    ],
    "webOrigins": [
        "http://localhost:5173",
        "http://localhost:3000"
    ],
    "attributes": {
        "pkce.code.challenge.method": "S256",
        "post.logout.redirect.uris": "http://localhost:5173/*##http://localhost:3000/*"
    }
}'

if client_exists "$ADMIN_TOKEN" "master" "meho-frontend"; then
    log_info "meho-frontend client already exists in master realm"
else
    log_info "Creating meho-frontend client in master realm..."
    create_client "$ADMIN_TOKEN" "master" "$MEHO_FRONTEND_CLIENT"
    log_info "meho-frontend client created in master realm"
fi

# Add realm roles mapper to meho-frontend client (required for global_admin detection)
log_info "Configuring realm roles mapper for meho-frontend client..."
CLIENT_UUID=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/clients?clientId=meho-frontend" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.[0].id')

if [[ -n "$CLIENT_UUID" ]] && [[ "$CLIENT_UUID" != "null" ]]; then
    # Check if mapper already exists
    MAPPER_EXISTS=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/clients/${CLIENT_UUID}/protocol-mappers/models" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.[] | select(.name == "realm roles") | .name // empty')
    
    if [[ -z "$MAPPER_EXISTS" ]]; then
        ROLES_MAPPER='{
            "name": "realm roles",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-realm-role-mapper",
            "consentRequired": false,
            "config": {
                "multivalued": "true",
                "claim.name": "roles",
                "jsonType.label": "String",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true"
            }
        }'
        
        curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/master/clients/${CLIENT_UUID}/protocol-mappers/models" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "${ROLES_MAPPER}" > /dev/null
        log_info "Realm roles mapper added to meho-frontend client"
    else
        log_info "Realm roles mapper already exists"
    fi
else
    log_warn "Could not find meho-frontend client UUID"
fi

# Create global_admin role if it doesn't exist
log_info "Checking global_admin role..."
ROLE_EXISTS=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/roles/global_admin" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" 2>/dev/null | jq -r '.name // empty')

if [[ -z "$ROLE_EXISTS" ]]; then
    log_info "Creating global_admin role..."
    curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/master/roles" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"name": "global_admin", "description": "Global administrator - can manage all tenants"}' > /dev/null
    log_info "global_admin role created"
else
    log_info "global_admin role already exists"
fi

# Create superadmin user if it doesn't exist
log_info "Checking superadmin user..."
USER_EXISTS=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/users?username=superadmin" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.[0].username // empty')

if [[ -z "$USER_EXISTS" ]]; then
    log_info "Creating superadmin user..."
    
    # Create user
    curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/master/users" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{
            "username": "superadmin",
            "email": "superadmin@meho.local",
            "emailVerified": true,
            "enabled": true,
            "firstName": "Super",
            "lastName": "Admin",
            "credentials": [{"type": "password", "value": "superadmin", "temporary": false}]
        }' > /dev/null
    
    log_info "superadmin user created"
else
    log_info "superadmin user already exists"
fi

# Always ensure global_admin role is assigned to superadmin user
log_info "Ensuring global_admin role is assigned to superadmin..."
USER_ID=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/users?username=superadmin" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.[0].id')

if [[ -n "$USER_ID" ]]; then
    ROLE_ID=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/roles/global_admin" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.id')
    
    if [[ -n "$ROLE_ID" ]]; then
        # Check if role is already assigned
        HAS_ROLE=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/master/users/${USER_ID}/role-mappings/realm" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" | jq -r '.[] | select(.name == "global_admin") | .name // empty')
        
        if [[ -z "$HAS_ROLE" ]]; then
            curl -sf -X POST "${KEYCLOAK_URL}/admin/realms/master/users/${USER_ID}/role-mappings/realm" \
                -H "Authorization: Bearer ${ADMIN_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "[{\"id\": \"${ROLE_ID}\", \"name\": \"global_admin\"}]" > /dev/null
            log_info "global_admin role assigned to superadmin"
        else
            log_info "superadmin already has global_admin role"
        fi
    else
        log_warn "Could not find global_admin role ID"
    fi
else
    log_warn "Could not find superadmin user ID"
fi

# Step 3: Import tenant realms via REST API
echo ""
log_info "Checking tenant realms..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KEYCLOAK_CONFIG_DIR="${PROJECT_ROOT}/config/keycloak"

# Import meho-tenant realm if it doesn't exist
REALM_EXISTS=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/meho-tenant" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" 2>/dev/null | jq -r '.realm // empty')

if [[ -z "$REALM_EXISTS" ]]; then
    if [[ -f "${KEYCLOAK_CONFIG_DIR}/meho-tenant-realm.json" ]]; then
        log_info "Importing meho-tenant realm..."
        if curl -sf -X POST "${KEYCLOAK_URL}/admin/realms" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" \
            -H "Content-Type: application/json" \
            -d @"${KEYCLOAK_CONFIG_DIR}/meho-tenant-realm.json" > /dev/null; then
            log_info "meho-tenant realm imported successfully"
            disable_ssl_for_realm "$ADMIN_TOKEN" "meho-tenant"
            log_info "SSL disabled for meho-tenant realm"
        else
            log_warn "Failed to import meho-tenant realm"
        fi
    else
        log_warn "meho-tenant-realm.json not found at ${KEYCLOAK_CONFIG_DIR}"
    fi
else
    log_info "meho-tenant realm already exists"
    # Ensure SSL is disabled even for existing realm
    disable_ssl_for_realm "$ADMIN_TOKEN" "meho-tenant"
fi

# Ensure meho-profile scope exists (for username in tokens)
log_info "Ensuring meho-profile scope in meho-tenant realm..."
add_profile_scope_to_realm "$ADMIN_TOKEN" "meho-tenant"

# Import example-tenant realm if it doesn't exist
EXAMPLE_REALM_EXISTS=$(curl -sf -X GET "${KEYCLOAK_URL}/admin/realms/example-tenant" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" 2>/dev/null | jq -r '.realm // empty')

if [[ -z "$EXAMPLE_REALM_EXISTS" ]]; then
    if [[ -f "${KEYCLOAK_CONFIG_DIR}/example-tenant-realm.json" ]]; then
        log_info "Importing example-tenant realm..."
        if curl -sf -X POST "${KEYCLOAK_URL}/admin/realms" \
            -H "Authorization: Bearer ${ADMIN_TOKEN}" \
            -H "Content-Type: application/json" \
            -d @"${KEYCLOAK_CONFIG_DIR}/example-tenant-realm.json" > /dev/null; then
            log_info "example-tenant realm imported successfully"
            disable_ssl_for_realm "$ADMIN_TOKEN" "example-tenant"
            log_info "SSL disabled for example-tenant realm"
        else
            log_warn "Failed to import example-tenant realm"
        fi
    else
        log_warn "example-tenant-realm.json not found at ${KEYCLOAK_CONFIG_DIR}"
    fi
else
    log_info "example-tenant realm already exists"
    # Ensure SSL is disabled even for existing realm
    disable_ssl_for_realm "$ADMIN_TOKEN" "example-tenant"
fi

# Ensure meho-profile scope exists (for username in tokens)
log_info "Ensuring meho-profile scope in example-tenant realm..."
add_profile_scope_to_realm "$ADMIN_TOKEN" "example-tenant"

# Step 4: Seed tenant configuration in database
echo ""
log_info "Seeding tenant configuration in database..."

# Use docker exec if postgres is in a container, otherwise use psql directly
if docker ps --format '{{.Names}}' | grep -q 'meho-postgres'; then
    DB_CMD="docker exec meho-postgres-1 psql -U ${DB_USER} -d ${DB_NAME}"
else
    DB_CMD="PGPASSWORD=${DB_PASSWORD} psql -h ${DB_HOST} -p ${DB_PORT} -U ${DB_USER} -d ${DB_NAME}"
fi

# Create meho-tenant entry with email domains
log_info "Creating meho-tenant entry..."
eval "${DB_CMD}" -c "
INSERT INTO tenant_agent_config (
    id, tenant_id, display_name, is_active, subscription_tier, 
    email_domains, created_at, updated_at
)
VALUES (
    gen_random_uuid(), 
    'meho-tenant', 
    'MEHO Demo Tenant', 
    true, 
    'enterprise', 
    '[\"meho.local\"]'::jsonb,
    NOW(), 
    NOW()
)
ON CONFLICT (tenant_id) DO UPDATE SET 
    display_name = EXCLUDED.display_name,
    email_domains = EXCLUDED.email_domains,
    updated_at = NOW();
" 2>/dev/null || log_warn "Could not insert meho-tenant (may already exist)"

# Create example-tenant entry with email domains
log_info "Creating example-tenant entry..."
eval "${DB_CMD}" -c "
INSERT INTO tenant_agent_config (
    id, tenant_id, display_name, is_active, subscription_tier, 
    email_domains, created_at, updated_at
)
VALUES (
    gen_random_uuid(), 
    'example-tenant', 
    'Example Tenant', 
    true, 
    'free', 
    '[\"example.com\"]'::jsonb,
    NOW(), 
    NOW()
)
ON CONFLICT (tenant_id) DO UPDATE SET 
    display_name = EXCLUDED.display_name,
    email_domains = EXCLUDED.email_domains,
    updated_at = NOW();
" 2>/dev/null || log_warn "Could not insert example-tenant (may already exist)"

# =============================================================================
# Summary
# =============================================================================

echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}   Setup Complete!${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "Available test accounts:"
echo ""
echo -e "${BLUE}Global Admin (master realm):${NC}"
echo "  Email: superadmin@meho.local"
echo "  Password: superadmin"
echo "  Login via: 'Administrator? Sign in directly'"
echo ""
echo -e "${BLUE}MEHO Tenant (meho-tenant realm):${NC}"
echo "  Admin: admin@meho.local / admin123"
echo "  User:  user@meho.local / user123"
echo "  Login via: Enter email, then 'Continue'"
echo ""
echo -e "${BLUE}Example Tenant (example-tenant realm):${NC}"
echo "  Admin: admin@example.com / admin123"
echo "  User:  user@example.com / user123"
echo "  Login via: Enter email, then 'Continue'"
echo ""
echo "Keycloak Admin Console: ${KEYCLOAK_URL}/admin"
echo "  Username: ${KEYCLOAK_ADMIN}"
echo "  Password: ${KEYCLOAK_ADMIN_PASSWORD}"
echo ""

