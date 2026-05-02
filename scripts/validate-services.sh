#!/usr/bin/env bash
#
# Validate MEHO services are responding correctly after startup.
#
# Runs unauthenticated health probes (backend /health, frontend) and then a
# small set of authenticated API endpoints using a real JWT minted from the
# seeded Keycloak meho-tenant realm (password grant against the meho-frontend
# public client, same pattern as tests/support/auth.py).

set -euo pipefail

# --- Configuration (overridable via env) --------------------------------------

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:5173}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
KEYCLOAK_REALM="${KEYCLOAK_REALM:-meho-tenant}"
KEYCLOAK_CLIENT="${KEYCLOAK_CLIENT:-meho-frontend}"
SMOKE_USERNAME="${SMOKE_USERNAME:-user@meho.local}"
SMOKE_PASSWORD="${SMOKE_PASSWORD:-user123}"

SERVICES=(
    "MEHO Backend|${BACKEND_URL}/health|meho"
    "Frontend|${FRONTEND_URL}|meho-frontend"
)

# The API routes derive tenant_id from the JWT (see routes_knowledge.py,
# routes_connectors.py); a query-param tenant_id is ignored. Leaving it out
# keeps these URLs honest.
CRITICAL_ENDPOINTS=(
    "Knowledge Documents:${BACKEND_URL}/api/knowledge/documents?limit=10"
    "Knowledge Chunks:${BACKEND_URL}/api/knowledge/chunks?limit=10"
    "Connectors List:${BACKEND_URL}/api/connectors"
    "Chat Sessions:${BACKEND_URL}/api/chat/sessions?limit=10"
)

echo "🔍 Validating MEHO Services..."
echo "================================"

TEST_TOKEN=""

get_test_token() {
    echo -n "Getting test token from Keycloak... "

    local token_url="${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token"
    local response
    # Guard against `set -e` aborting on curl failure so we surface a friendly
    # diagnosis instead of a bare non-zero exit. Password is piped via stdin
    # (`password@-`) so it never appears in `ps` argv.
    if ! response=$(printf '%s' "${SMOKE_PASSWORD}" | curl -s --max-time 15 \
        -X POST \
        -H "Content-Type: application/x-www-form-urlencoded" \
        --data-urlencode "grant_type=password" \
        --data-urlencode "client_id=${KEYCLOAK_CLIENT}" \
        --data-urlencode "username=${SMOKE_USERNAME}" \
        --data-urlencode "password@-" \
        "${token_url}"); then
        echo "❌ FAILED (Could not reach Keycloak token endpoint)"
        echo "   URL: ${token_url}"
        return 1
    fi

    if [[ -z "$response" ]]; then
        echo "❌ FAILED (Keycloak token endpoint returned no response)"
        echo "   URL: ${token_url}"
        return 1
    fi

    if command -v jq >/dev/null 2>&1; then
        TEST_TOKEN=$(printf '%s' "$response" | jq -r '.access_token // empty')
    else
        TEST_TOKEN=$(printf '%s' "$response" \
            | grep -o '"access_token"[[:space:]]*:[[:space:]]*"[^"]*"' \
            | head -n1 \
            | sed 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
    fi

    if [[ -z "$TEST_TOKEN" ]]; then
        echo "❌ FAILED (Could not extract access_token)"
        # Redact token fields before echoing; Keycloak error responses don't
        # include tokens, but a partial/garbled response could.
        local redacted
        if command -v jq >/dev/null 2>&1; then
            redacted=$(printf '%s' "$response" \
                | jq -r '[.error, .error_description] | map(select(. != null and . != "")) | join(": ")')
            if [[ -n "$redacted" ]]; then
                echo "   Keycloak error: ${redacted}"
            else
                echo "   Response could not be parsed; check Keycloak logs."
            fi
        else
            redacted=$(printf '%s' "$response" \
                | sed -E 's/"(access_token|refresh_token|id_token)"[[:space:]]*:[[:space:]]*"[^"]*"/"\1":"REDACTED"/g')
            echo "   Response (redacted): ${redacted}"
        fi
        return 1
    fi

    echo "✅ OK"
    return 0
}

check_endpoint() {
    local name=$1
    local url=$2
    local service=$3
    local use_auth="${4:-}"

    echo -n "Testing $name... "

    local response
    for i in 1 2 3; do
        # Guard against `set -e` aborting on curl failure so the retry loop
        # actually retries transient connection errors.
        if [[ "$use_auth" == "true" ]] && [[ -n "$TEST_TOKEN" ]]; then
            if ! response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
                -H "Authorization: Bearer $TEST_TOKEN" "$url" 2>/dev/null); then
                response=""
            fi
        else
            if ! response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null); then
                response=""
            fi
        fi

        if [[ -n "$response" ]]; then
            if [[ "$response" == "200" ]]; then
                echo "✅ OK"
                return 0
            elif [[ "$response" == "404" ]]; then
                echo "❌ FAILED (404 - endpoint not found)"
                echo "   URL: $url"
                if [[ -n "$service" ]]; then
                    echo "   Check logs: docker logs meho-$service-1"
                fi
                return 1
            elif [[ "$response" == "500" ]]; then
                echo "❌ FAILED (500 - internal error)"
                echo "   URL: $url"
                if [[ -n "$service" ]]; then
                    echo "   Check logs: docker logs meho-$service-1 --tail 20"
                fi
                return 1
            elif [[ "$use_auth" == "true" ]] && [[ "$response" == "401" || "$response" == "403" ]]; then
                # The whole point of this script is catching silent auth
                # failures; a rejected JWT must fail loud, not pass as WARNING.
                echo "❌ FAILED (HTTP $response - authentication rejected)"
                echo "   URL: $url"
                if [[ -n "$service" ]]; then
                    echo "   Check logs: docker logs meho-$service-1 --tail 20"
                fi
                return 1
            else
                echo "⚠️  WARNING (HTTP $response)"
                return 0
            fi
        fi

        if [[ $i -lt 3 ]]; then
            sleep 2
        fi
    done

    echo "❌ FAILED (connection timeout)"
    echo "   URL: $url"
    if [[ -n "$service" ]]; then
        echo "   Is service running? docker ps | grep $service"
    fi
    return 1
}

# Check health endpoints
echo ""
echo "1️⃣  Checking Service Health Endpoints..."
echo "----------------------------------------"

HEALTH_FAILED=0
for service_info in "${SERVICES[@]}"; do
    name="${service_info%%|*}"
    rest="${service_info#*|}"
    url="${rest%%|*}"
    service="${rest#*|}"

    if ! check_endpoint "$name" "$url" "$service"; then
        HEALTH_FAILED=$((HEALTH_FAILED + 1))
    fi
done

# Check critical API endpoints (with authentication)
echo ""
echo "2️⃣  Checking Critical API Endpoints..."
echo "----------------------------------------"

API_FAILED=0
if ! get_test_token; then
    echo ""
    echo "⚠️  Cannot check authenticated endpoints without token"
    echo "    Skipping critical endpoint checks..."
else
    echo ""
    for endpoint_info in "${CRITICAL_ENDPOINTS[@]}"; do
        name="${endpoint_info%%:*}"
        url="${endpoint_info#*:}"

        if ! check_endpoint "$name" "$url" "" "true"; then
            API_FAILED=$((API_FAILED + 1))
        fi
    done
fi

# Summary
echo ""
echo "================================"
echo "📊 Validation Summary"
echo "================================"

if [[ $HEALTH_FAILED -eq 0 ]] && [[ $API_FAILED -eq 0 ]]; then
    echo "✅ All services are healthy and responding correctly!"
    echo ""
    echo "🚀 Ready for use:"
    echo "   Frontend: ${FRONTEND_URL}"
    echo "   API Docs: ${BACKEND_URL}/docs"
    echo ""
    exit 0
else
    echo "❌ Validation failed:"
    echo "   Health checks failed: $HEALTH_FAILED"
    echo "   API endpoint checks failed: $API_FAILED"
    echo ""
    echo "🔧 Troubleshooting:"
    echo "   1. Check service logs: docker logs meho-meho-1"
    echo "   2. Verify services are running: docker ps"
    echo "   3. Check for port conflicts: lsof -i :8000"
    echo ""
    exit 1
fi
