#!/bin/bash
# ==============================================================================
# MEHO Kubernetes Service Account Creator
# ==============================================================================
# 
# Creates a Service Account in your Kubernetes cluster with read access,
# generates a long-lived token, and outputs the connection details for MEHO.
#
# Usage:
#   ./create-k8s-service-account.sh [namespace] [token-duration] [role]
#
# Arguments:
#   namespace       - Kubernetes namespace (default: kube-system)
#   token-duration  - Token validity period (default: 8760h = 1 year)
#   role            - ClusterRole to bind (default: view)
#
# Examples:
#   # Create with defaults (read-only access, 1 year token)
#   ./create-k8s-service-account.sh
#
#   # Create in default namespace with 30-day token
#   ./create-k8s-service-account.sh default 720h
#
#   # Create with cluster-admin access (full control)
#   ./create-k8s-service-account.sh kube-system 8760h cluster-admin
#
# Prerequisites:
#   - kubectl configured with cluster access
#   - Permissions to create ServiceAccounts and ClusterRoleBindings
#
# ==============================================================================

set -e

# Configuration
NAMESPACE=${1:-kube-system}
DURATION=${2:-8760h}  # 1 year default
ROLE=${3:-view}       # read-only by default
SA_NAME="meho-agent"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} MEHO Kubernetes Service Account Setup ${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Check kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}Error: kubectl is not installed or not in PATH${NC}"
    exit 1
fi

# Check cluster connectivity
echo -e "${YELLOW}Checking cluster connectivity...${NC}"
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to Kubernetes cluster${NC}"
    echo "Please ensure kubectl is configured correctly."
    exit 1
fi

CLUSTER_NAME=$(kubectl config current-context)
echo -e "${GREEN}✓ Connected to cluster: ${CLUSTER_NAME}${NC}"
echo ""

# Create namespace if it doesn't exist (unless it's kube-system)
if [ "$NAMESPACE" != "kube-system" ] && [ "$NAMESPACE" != "default" ]; then
    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        echo -e "${YELLOW}Creating namespace: ${NAMESPACE}${NC}"
        kubectl create namespace "$NAMESPACE"
    fi
fi

# Create Service Account
echo -e "${YELLOW}Creating ServiceAccount: ${SA_NAME} in namespace ${NAMESPACE}${NC}"
kubectl create serviceaccount "$SA_NAME" -n "$NAMESPACE" 2>/dev/null || \
    echo -e "${BLUE}ServiceAccount already exists${NC}"

# Create ClusterRoleBinding
BINDING_NAME="meho-agent-${ROLE}"
echo -e "${YELLOW}Creating ClusterRoleBinding: ${BINDING_NAME} with role: ${ROLE}${NC}"
kubectl create clusterrolebinding "$BINDING_NAME" \
    --clusterrole="$ROLE" \
    --serviceaccount="${NAMESPACE}:${SA_NAME}" 2>/dev/null || \
    echo -e "${BLUE}ClusterRoleBinding already exists${NC}"

# Generate token
echo -e "${YELLOW}Generating token (valid for ${DURATION})...${NC}"
TOKEN=$(kubectl create token "$SA_NAME" -n "$NAMESPACE" --duration="$DURATION")

# Get server URL
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

# Get cluster version
VERSION=$(kubectl version --short 2>/dev/null | grep "Server" | cut -d: -f2 | tr -d ' ' || echo "unknown")

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  MEHO Kubernetes Configuration Ready  ${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}Cluster:${NC} ${CLUSTER_NAME}"
echo -e "${BLUE}Version:${NC} ${VERSION}"
echo -e "${BLUE}Role:${NC} ${ROLE}"
echo -e "${BLUE}Token Validity:${NC} ${DURATION}"
echo ""
echo -e "${BLUE}API Server URL:${NC}"
echo "$SERVER"
echo ""
echo -e "${BLUE}Service Account Token:${NC}"
echo "$TOKEN"
echo ""
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "1. Go to MEHO → Connectors → Create New Connector"
echo "2. Select 'Kubernetes' as the connector type"
echo "3. Paste the API Server URL and Token above"
echo "4. Enable 'Skip TLS Verification' if using self-signed certs"
echo ""

# Security notice for cluster-admin
if [ "$ROLE" == "cluster-admin" ]; then
    echo -e "${RED}⚠️  WARNING: This token has cluster-admin privileges!${NC}"
    echo -e "${RED}   Only use in trusted environments.${NC}"
    echo ""
fi

echo -e "${GREEN}Done! You can now add this cluster to MEHO.${NC}"

