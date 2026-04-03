# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
API schemas for Ingestion Service.

Defines request/response models for webhook endpoints.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WebhookEventRequest(BaseModel):
    """Generic webhook event request"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_type": "push",
                "source_system": "github",
                "payload": {
                    "repository": "myorg/myrepo",
                    "ref": "refs/heads/main",
                    "commits": [{"id": "abc123", "message": "Update docs"}],
                },
                "timestamp": "2025-11-16T10:30:00Z",
            }
        }
    )

    event_type: str = Field(..., description="Type of event (e.g., 'push', 'sync', 'pod_crash')")
    source_system: str = Field(..., description="System that generated the event")
    payload: dict[str, Any] = Field(..., description="Event payload")
    timestamp: datetime | None = Field(default=None, description="Event timestamp")


class GitHubPushEvent(BaseModel):
    """GitHub push event webhook payload"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ref": "refs/heads/main",
                "repository": {
                    "name": "myrepo",
                    "full_name": "myorg/myrepo",
                    "url": "https://github.com/myorg/myrepo",
                },
                "commits": [
                    {
                        "id": "abc123",
                        "message": "Update documentation",
                        "added": ["docs/api.md"],
                        "modified": ["docs/intro.md"],
                        "removed": [],
                    }
                ],
                "pusher": {"name": "john_doe", "email": "john@example.com"},
            }
        }
    )

    ref: str = Field(..., description="Git ref (e.g., 'refs/heads/main')")
    repository: dict[str, Any] = Field(..., description="Repository information")
    commits: list[dict[str, Any]] = Field(..., description="List of commits")
    pusher: dict[str, str] = Field(..., description="User who pushed")


class ArgoCDSyncEvent(BaseModel):
    """ArgoCD sync status event"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "app_name": "my-app",
                "sync_status": "Synced",
                "health_status": "Degraded",
                "revision": "abc123",
                "message": "Pods failing health checks",
            }
        }
    )

    app_name: str = Field(..., description="Application name")
    sync_status: str = Field(..., description="Sync status (Synced, OutOfSync, Unknown)")
    health_status: str = Field(..., description="Health status (Healthy, Degraded, Progressing)")
    revision: str = Field(..., description="Git revision")
    message: str | None = Field(None, description="Status message")


class KubernetesPodEvent(BaseModel):
    """Kubernetes pod event"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "pod_name": "my-app-7d8f5b6c9-x2k4p",
                "namespace": "production",
                "event_type": "CrashLoopBackOff",
                "reason": "Error",
                "message": "Container exited with code 1",
            }
        }
    )

    pod_name: str = Field(..., description="Pod name")
    namespace: str = Field(..., description="Kubernetes namespace")
    event_type: str = Field(..., description="Event type (CrashLoopBackOff, Pending, etc.)")
    reason: str = Field(..., description="Event reason")
    message: str = Field(..., description="Event message")


class WebhookResponse(BaseModel):
    """Webhook response"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "accepted",
                "message": "Event queued for processing",
                "event_id": "evt_abc123",
            }
        }
    )

    status: str = Field(..., description="Status of webhook processing")
    message: str = Field(..., description="Response message")
    event_id: str | None = Field(None, description="Assigned event ID for tracking")


class IngestionStatusResponse(BaseModel):
    """Ingestion status response"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event_id": "evt_abc123",
                "status": "completed",
                "chunks_created": 5,
                "error": None,
            }
        }
    )

    event_id: str = Field(..., description="Event ID")
    status: str = Field(..., description="Processing status")
    chunks_created: int | None = Field(None, description="Number of knowledge chunks created")
    error: str | None = Field(None, description="Error message if failed")


class HealthResponse(BaseModel):
    """Health check response"""

    model_config = ConfigDict(
        json_schema_extra={"example": {"status": "healthy", "version": "0.1.0"}}
    )

    status: str = Field(..., description="Service status")
    version: str = Field(..., description="Service version")
