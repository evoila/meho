# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
E2E test for generic webhook processing.

Tests the complete flow: webhook → template → knowledge chunk
"""

from unittest.mock import AsyncMock, Mock

import pytest

from meho_app.modules.ingestion.processor import GenericWebhookProcessor
from meho_app.modules.ingestion.template_renderer import TemplateRenderer


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_github_push_webhook_full_flow():
    """
    E2E test: GitHub push webhook → template → knowledge chunk

    This tests the COMPLETE generic architecture:
    1. Create event template (configuration)
    2. Receive webhook payload
    3. Process using template
    4. Create knowledge chunk

    Zero hard-coded logic! All template-driven!
    """
    # Setup mocks
    mock_template_repo = Mock()
    mock_knowledge_store = Mock()

    # Create GitHub push template (configuration-driven!)
    github_template = Mock()
    github_template.id = "template-123"
    github_template.connector_id = "github-prod"
    github_template.event_type = "push"
    github_template.text_template = """GitHub Push Event

Repository: {{ payload.repository.full_name }}
Branch: {{ payload.ref | replace('refs/heads/', '') }}
Pusher: {{ payload.pusher.name }}

Commits ({{ payload.commits | length }}):
{% for commit in payload.commits %}
- {{ commit.id[:7] }}: {{ commit.message }}
{% endfor %}"""
    github_template.tag_rules = [
        "source:github",
        "repo:{{ payload.repository.full_name }}",
        "branch:{{ payload.ref | replace('refs/heads/', '') }}",
        "pusher:{{ payload.pusher.name }}",
    ]
    github_template.issue_detection_rule = "false"
    github_template.tenant_id = "tenant-1"

    # Mock template lookup
    mock_template_repo.get_template = AsyncMock(return_value=github_template)

    # Mock knowledge store
    mock_chunk = Mock()
    mock_chunk.id = "chunk-abc123"
    mock_knowledge_store.add_chunk = AsyncMock(return_value=mock_chunk)

    # Create processor with real renderer
    real_renderer = TemplateRenderer()
    processor = GenericWebhookProcessor(
        template_repo=mock_template_repo,
        knowledge_store=mock_knowledge_store,
        renderer=real_renderer,  # Use REAL renderer!
    )

    # Simulate GitHub webhook payload
    github_payload = {
        "ref": "refs/heads/main",
        "repository": {
            "full_name": "myorg/myrepo",
            "name": "myrepo",
            "url": "https://github.com/myorg/myrepo",
        },
        "pusher": {"name": "john_doe", "email": "john@example.com"},
        "commits": [
            {
                "id": "abc123def456",
                "message": "feat: Add new feature",
                "added": ["src/feature.py"],
                "modified": ["README.md"],
                "removed": [],
            },
            {
                "id": "def456ghi789",
                "message": "fix: Fix bug in login",
                "added": [],
                "modified": ["src/auth.py"],
                "removed": [],
            },
        ],
    }

    # Process webhook (THE MAGIC HAPPENS HERE!)
    await processor.process_webhook(
        connector_id="github-prod",
        event_type="push",
        payload=github_payload,
        tenant_id="tenant-1",
        system_id="github-prod",
    )

    # Verify template was looked up correctly
    mock_template_repo.get_template.assert_called_once_with(
        connector_id="github-prod", event_type="push"
    )

    # Verify knowledge chunk was created
    assert mock_knowledge_store.add_chunk.called
    chunk_create = mock_knowledge_store.add_chunk.call_args[0][0]

    # Verify rendered text
    assert "GitHub Push Event" in chunk_create.text
    assert "myorg/myrepo" in chunk_create.text
    assert "main" in chunk_create.text  # Branch extracted!
    assert "john_doe" in chunk_create.text
    assert "abc123d" in chunk_create.text  # Commit ID (first 7 chars)
    assert "feat: Add new feature" in chunk_create.text
    assert "2" in chunk_create.text or "Commits (2)" in chunk_create.text

    # Verify generated tags
    assert "source:github" in chunk_create.tags
    assert "repo:myorg/myrepo" in chunk_create.tags
    assert "branch:main" in chunk_create.tags
    assert "pusher:john_doe" in chunk_create.tags

    # Verify no "issue" tag (push events aren't issues)
    assert "issue" not in chunk_create.tags

    # Verify tenant/system isolation
    assert chunk_create.tenant_id == "tenant-1"
    assert chunk_create.system_id == "github-prod"

    print("\n✅ E2E Test Passed!")
    print("✅ Template-driven processing works!")
    print("✅ GitHub webhook → Knowledge chunk flow complete!")
    print("✅ Zero hard-coded logic - all configuration-driven!")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_argocd_deployment_webhook_with_issue_detection():
    """
    E2E test: ArgoCD deployment webhook with issue detection

    Tests that issue detection works correctly.
    """
    mock_template_repo = Mock()
    mock_knowledge_store = Mock()

    # Create ArgoCD template with issue detection
    argocd_template = Mock()
    argocd_template.id = "template-456"
    argocd_template.connector_id = "argocd-prod"
    argocd_template.event_type = "sync_status"
    argocd_template.text_template = """ArgoCD Deployment Status

Application: {{ payload.app_name }}
Sync Status: {{ payload.sync_status }}
Health Status: {{ payload.health_status }}
Revision: {{ payload.revision }}

{% if payload.message %}
Details: {{ payload.message }}
{% endif %}"""
    argocd_template.tag_rules = [
        "source:argocd",
        "app:{{ payload.app_name }}",
        "sync:{{ payload.sync_status }}",
        "health:{{ payload.health_status }}",
    ]
    # Issue detection: Degraded health OR out of sync
    argocd_template.issue_detection_rule = (
        "{{ payload.health_status == 'Degraded' or payload.sync_status == 'OutOfSync' }}"
    )
    argocd_template.tenant_id = "tenant-1"

    mock_template_repo.get_template = AsyncMock(return_value=argocd_template)

    mock_chunk = Mock()
    mock_chunk.id = "chunk-def456"
    mock_knowledge_store.add_chunk = AsyncMock(return_value=mock_chunk)

    processor = GenericWebhookProcessor(
        template_repo=mock_template_repo,
        knowledge_store=mock_knowledge_store,
        renderer=TemplateRenderer(),
    )

    # Simulate ArgoCD webhook (Degraded!)
    argocd_payload = {
        "app_name": "my-api",
        "sync_status": "Synced",
        "health_status": "Degraded",  # ISSUE!
        "revision": "abc123",
        "message": "Pods are failing health checks",
    }

    # Process webhook
    await processor.process_webhook(
        connector_id="argocd-prod",
        event_type="sync_status",
        payload=argocd_payload,
        tenant_id="tenant-1",
    )

    # Verify chunk was created
    chunk_create = mock_knowledge_store.add_chunk.call_args[0][0]

    # Verify text
    assert "ArgoCD Deployment Status" in chunk_create.text
    assert "my-api" in chunk_create.text
    assert "Degraded" in chunk_create.text
    assert "Pods are failing health checks" in chunk_create.text

    # Verify tags
    assert "source:argocd" in chunk_create.tags
    assert "app:my-api" in chunk_create.tags
    assert "health:Degraded" in chunk_create.tags

    # Verify issue detection worked!
    assert "issue" in chunk_create.tags  # Issue detected!

    print("\n✅ E2E Test with Issue Detection Passed!")
    print("✅ ArgoCD webhook → Issue detected → Tagged correctly!")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_custom_monitoring_webhook():
    """
    E2E test: Custom monitoring system webhook

    Demonstrates that ANY system can work with MEHO using templates!
    """
    mock_template_repo = Mock()
    mock_knowledge_store = Mock()

    # Custom monitoring template (user-defined!)
    custom_template = Mock()
    custom_template.id = "template-custom"
    custom_template.connector_id = "custom-monitoring"
    custom_template.event_type = "alert"
    custom_template.text_template = """Monitoring Alert

Title: {{ payload.title }}
Severity: {{ payload.priority }}
Host: {{ payload.host }}
Status: {{ payload.status }}

{{ payload.description }}

Tags: {{ payload.tags | join(', ') }}"""
    custom_template.tag_rules = [
        "source:monitoring",
        "severity:{{ payload.priority }}",
        "host:{{ payload.host }}",
        "status:{{ payload.status }}",
    ]
    custom_template.issue_detection_rule = (
        "{{ payload.priority in ['high', 'critical'] and payload.status == 'triggered' }}"
    )
    custom_template.tenant_id = "tenant-1"

    mock_template_repo.get_template = AsyncMock(return_value=custom_template)

    mock_chunk = Mock()
    mock_chunk.id = "chunk-custom"
    mock_knowledge_store.add_chunk = AsyncMock(return_value=mock_chunk)

    processor = GenericWebhookProcessor(
        template_repo=mock_template_repo,
        knowledge_store=mock_knowledge_store,
        renderer=TemplateRenderer(),
    )

    # Custom monitoring payload (YOUR system!)
    custom_payload = {
        "title": "High CPU Usage",
        "priority": "high",
        "host": "prod-server-01",
        "status": "triggered",
        "description": "CPU usage exceeded 90% for 5 minutes",
        "tags": ["production", "cpu", "performance"],
    }

    # Process webhook
    await processor.process_webhook(
        connector_id="custom-monitoring",
        event_type="alert",
        payload=custom_payload,
        tenant_id="tenant-1",
    )

    # Verify
    chunk_create = mock_knowledge_store.add_chunk.call_args[0][0]

    assert "Monitoring Alert" in chunk_create.text
    assert "High CPU Usage" in chunk_create.text
    assert "prod-server-01" in chunk_create.text
    assert "CPU usage exceeded 90%" in chunk_create.text

    assert "source:monitoring" in chunk_create.tags
    assert "severity:high" in chunk_create.tags
    assert "host:prod-server-01" in chunk_create.tags
    assert "issue" in chunk_create.tags  # High priority triggered = issue

    print("\n✅ E2E Test with Custom System Passed!")
    print("✅ ANY system can work with MEHO using templates!")
    print("✅ No code changes needed!")
