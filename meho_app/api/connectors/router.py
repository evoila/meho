# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector management API router.

Combines all connector-related operations into a single router.
The router has a /connectors prefix, and all sub-routes are relative to this.
"""

from fastapi import APIRouter

# Import all route handlers
from meho_app.api.connectors.operations import (
    alertmanager,
    argocd,
    aws,
    azure,
    confluence,
    connector_operations,
    credentials,
    crud,
    email,
    endpoints,
    events,
    export_import,
    gcp,
    github,
    health,
    jira,
    kubernetes,
    loki,
    mcp,
    memories,
    prometheus,
    proxmox,
    skills,
    slack,
    soap,
    specs,
    tempo,
    testing,
    types,
    vmware,
)

# Create main router with prefix
router = APIRouter(prefix="/connectors", tags=["connectors"])

# Include all sub-routers using include_router() for proper path resolution
# Note: We must specify prefix="" explicitly for routers with empty-path routes
# to avoid FastAPI's "Prefix and path cannot be both empty" error

# VMware-specific routes (POST /vmware)
router.include_router(vmware.router, prefix="")

# Proxmox-specific routes (POST /proxmox)
router.include_router(proxmox.router, prefix="")

# Kubernetes-specific routes (POST /kubernetes)
router.include_router(kubernetes.router, prefix="")

# GCP-specific routes (POST /gcp)
router.include_router(gcp.router, prefix="")

# Prometheus-specific routes (POST /prometheus)
router.include_router(prometheus.router, prefix="")

# Loki-specific routes (POST /loki)
router.include_router(loki.router, prefix="")

# Tempo-specific routes (POST /tempo)
router.include_router(tempo.router, prefix="")

# Alertmanager-specific routes (POST /alertmanager)
router.include_router(alertmanager.router, prefix="")

# Jira-specific routes (POST /jira)
router.include_router(jira.router, prefix="")

# Confluence-specific routes (POST /confluence)
router.include_router(confluence.router, prefix="")

# Email-specific routes (POST /email, GET /{connector_id}/email-history)
router.include_router(email.router, prefix="")

# ArgoCD-specific routes (POST /argocd)
router.include_router(argocd.router, prefix="")

# GitHub-specific routes (POST /github)
router.include_router(github.router, prefix="")

# Azure-specific routes (POST /azure)
router.include_router(azure.router, prefix="")

# AWS-specific routes (POST /aws)
router.include_router(aws.router, prefix="")
# MCP-specific routes (POST /mcp)
router.include_router(mcp.router, prefix="")

# Slack-specific routes (POST /slack)
router.include_router(slack.router, prefix="")

# Health routes (GET /health) -- must be before CRUD to avoid /{id} capture
router.include_router(health.router, prefix="")

# CRUD routes (GET "", POST "", GET /{id}, PATCH /{id}, DELETE /{id})
router.include_router(crud.router, prefix="")

# Spec routes (POST /{id}/openapi-spec, GET /{id}/openapi-spec/download)
router.include_router(specs.router, prefix="")

# Credentials routes (POST /{id}/credentials, GET /{id}/credentials/status, DELETE /{id}/credentials)
router.include_router(credentials.router, prefix="")

# Testing routes (POST /{id}/test-connection, POST /{id}/test-auth)
router.include_router(testing.router, prefix="")

# Endpoints routes (GET /{id}/endpoints, GET /{id}/endpoints/{endpoint_id}, POST /{id}/endpoints/{endpoint_id}/test)
router.include_router(endpoints.router, prefix="")

# Operations routes (GET /{id}/operations)
router.include_router(connector_operations.router, prefix="")

# Types routes (GET /{id}/types)
router.include_router(types.router, prefix="")

# SOAP routes (POST /{id}/wsdl, GET /{id}/soap-operations, etc.)
router.include_router(soap.router, prefix="")

# Export/Import routes (POST /export, POST /import)
router.include_router(export_import.router, prefix="")

# Skill routes (PUT /{id}/skill, POST /{id}/skill/regenerate)
router.include_router(skills.router, prefix="")

# Memory routes (GET/POST/PATCH/DELETE /{connector_id}/memories/...)
router.include_router(memories.router, prefix="")

# Event routes (GET/POST/PATCH/DELETE /{connector_id}/events/...)
router.include_router(events.router, prefix="")
