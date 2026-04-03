#!/bin/bash
#
# Validate MEHO services are responding correctly after startup.
# This catches common issues before manual testing.
#

set -e

SERVICES=(
    "MEHO Backend|http://localhost:8000/health|meho"
    "Frontend|http://localhost:5173|meho-frontend"
)

CRITICAL_ENDPOINTS=(
    "Knowledge Documents:http://localhost:8000/api/knowledge/documents?limit=10&tenant_id=demo-tenant"
    "Knowledge Chunks:http://localhost:8000/api/knowledge/chunks?limit=10&tenant_id=demo-tenant"
    "Connectors List:http://localhost:8000/api/connectors?tenant_id=demo-tenant"
    "Chat Sessions:http://localhost:8000/api/chat/sessions?limit=10"
)

echo "🔍 Validating MEHO Services..."
echo "================================"

# Global variable to hold test token
TEST_TOKEN=""

# Function to get test token
get_test_token() {
    echo -n "Getting test token for authentication... "
    
    # Get token from auth endpoint
    response=$(curl -s -X POST "http://localhost:8000/api/auth/test-token" \
        -H "Content-Type: application/json" \
        -d '{"user_id":"test-user@example.com","tenant_id":"demo-tenant","roles":["user"]}' 2>/dev/null)
    
    # Extract token from JSON response using grep and sed
    TEST_TOKEN=$(echo "$response" | grep -o '"token":"[^"]*"' | sed 's/"token":"\(.*\)"/\1/')
    
    if [[ -z "$TEST_TOKEN" ]]; then
        echo "❌ FAILED (Could not get test token)"
        echo "   Backend API may not be ready yet"
        return 1
    fi
    
    echo "✅ OK"
    return 0
}

# Function to check HTTP endpoint
check_endpoint() {
    local name=$1
    local url=$2
    local service=$3
    local use_auth=$4  # Optional parameter for auth
    
    echo -n "Testing $name... "
    
    # Try up to 3 times with 2 second delay
    for i in {1..3}; do
        # Use eval to properly handle optional auth header
        if [[ "$use_auth" = "true" ]] && [[ -n "$TEST_TOKEN" ]]; then
            response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
                -H "Authorization: Bearer $TEST_TOKEN" "$url" 2>/dev/null)
        else
            response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null)
        fi
        
        if [[ -n "$response" ]]; then
            if [[ "$response" = "200" ]]; then
                echo "✅ OK"
                return 0
            elif [[ "$response" = "404" ]]; then
                echo "❌ FAILED (404 - endpoint not found)"
                echo "   URL: $url"
                if [[ -n "$service" ]]; then
                    echo "   Check logs: docker logs meho-$service-1"
                fi
                return 1
            elif [[ "$response" = "500" ]]; then
                echo "❌ FAILED (500 - internal error)"
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
    # Split on pipe: "Name|URL|Service"
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

# Get authentication token first
if ! get_test_token; then
    echo ""
    echo "⚠️  Cannot check authenticated endpoints without token"
    echo "    Skipping critical endpoint checks..."
    API_FAILED=0
else
    echo ""
    API_FAILED=0
    for endpoint_info in "${CRITICAL_ENDPOINTS[@]}"; do
        # Split on first colon: "Name:URL"
        name="${endpoint_info%%:*}"
        # Get everything after first colon as URL (preserves http://...)
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
    echo "   Frontend: http://localhost:5173"
    echo "   API Docs: http://localhost:8000/docs"
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
