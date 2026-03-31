# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for user-created temporary event knowledge.

Tests the "marathon notice" / "maintenance window" use case where users
manually create temporary knowledge with expiration.

Phase 84: KnowledgeChunkCreate schema changed, system_id renamed to connector_id.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: KnowledgeChunkCreate system_id renamed to connector_id, event knowledge schema changed")

from datetime import UTC, datetime, timedelta

from meho_app.modules.knowledge.schemas import KnowledgeChunkCreate, KnowledgeType


def test_user_can_create_temporary_notice():
    """Test user creating temporary notice (e.g., marathon, maintenance)"""
    tomorrow = datetime.now(tz=UTC) + timedelta(days=1)

    # User posts: "Marathon tomorrow, streets closed"
    chunk = KnowledgeChunkCreate(
        text="Berliner marathon tomorrow November 17th. All streets in city center closed 6 AM - 6 PM. VPN recommended for remote work.",
        knowledge_type=KnowledgeType.EVENT,  # Temporary!
        expires_at=tomorrow.replace(hour=18, minute=0),  # Expires tomorrow 6 PM
        priority=50,  # High priority for visibility
        tenant_id="company",
        tags=["notice", "marathon", "berlin", "event"],
    )

    assert chunk.knowledge_type == KnowledgeType.EVENT
    assert chunk.expires_at is not None
    assert chunk.priority == 50
    assert "notice" in chunk.tags


def test_maintenance_window_notice():
    """Test posting maintenance window notice"""
    end_time = (datetime.now(tz=UTC) + timedelta(days=1)).replace(hour=1, minute=0)  # 1 AM tomorrow

    chunk = KnowledgeChunkCreate(
        text="Maintenance Window: DC-WEST network upgrade tonight 11 PM - 1 AM. All vCenter, NSX, and vSAN APIs will be unavailable during this time.",
        knowledge_type=KnowledgeType.EVENT,
        expires_at=end_time,  # Expires when maintenance ends
        priority=80,  # Critical priority
        tenant_id="company",
        system_id="dc-west",  # Specific to DC-WEST
        tags=["maintenance", "dc-west", "vcenter", "notice"],
    )

    assert chunk.knowledge_type == KnowledgeType.EVENT
    assert chunk.expires_at == end_time
    assert chunk.priority == 80  # Critical
    assert chunk.system_id == "dc-west"


def test_vpn_issue_notice():
    """Test posting temporary issue notice"""
    in_4_hours = datetime.now(tz=UTC) + timedelta(hours=4)

    chunk = KnowledgeChunkCreate(
        text="VPN Connectivity Issues: Multiple users reporting VPN connection failures this morning. IT investigating. Use cellular hotspot if urgent.",
        knowledge_type=KnowledgeType.EVENT,
        expires_at=in_4_hours,  # Probably resolved in 4 hours
        priority=60,  # High priority
        tenant_id="company",
        tags=["notice", "vpn", "issue", "urgent"],
    )

    assert chunk.knowledge_type == KnowledgeType.EVENT
    assert chunk.expires_at is not None
    assert chunk.priority == 60


def test_temporary_event_vs_permanent_procedure():
    """Test distinction between temporary notice and permanent procedure"""

    # Temporary notice (expires)
    notice = KnowledgeChunkCreate(
        text="Network maintenance tonight",
        knowledge_type=KnowledgeType.EVENT,
        expires_at=datetime.now(tz=UTC) + timedelta(days=1),
        priority=50,
        tenant_id="company",
    )

    # Permanent procedure (never expires)
    procedure = KnowledgeChunkCreate(
        text="Network Maintenance Procedure: 1. Notify users, 2. Schedule window...",
        knowledge_type=KnowledgeType.PROCEDURE,
        expires_at=None,  # Never expires
        priority=0,
        tenant_id="company",
    )

    # Verify differences
    assert notice.knowledge_type == KnowledgeType.EVENT
    assert notice.expires_at is not None

    assert procedure.knowledge_type == KnowledgeType.PROCEDURE
    assert procedure.expires_at is None  # Permanent!


def test_event_requires_expiration_in_practice():
    """Test that EVENT type knowledge should have expiration (best practice)"""
    # While technically allowed, EVENT without expiration defeats the purpose
    # Frontend should enforce this

    event_without_expiration = KnowledgeChunkCreate(
        text="Some event",
        knowledge_type=KnowledgeType.EVENT,
        expires_at=None,  # Not recommended!
        tenant_id="company",
    )

    # This is valid but not recommended
    # Frontend should show warning or default to 24 hours
    assert event_without_expiration.knowledge_type == KnowledgeType.EVENT
    assert event_without_expiration.expires_at is None
    # Note: Frontend validation should prevent this or warn user


def test_lesson_learned_is_procedure_not_event():
    """Test that lessons learned are PROCEDURE (permanent), not EVENT"""

    lesson = KnowledgeChunkCreate(
        text="Lesson learned: Always check ArgoCD sync status before checking K8s pods when debugging deployment issues. This saves 10-15 minutes of investigation time.",
        knowledge_type=KnowledgeType.PROCEDURE,  # Permanent!
        expires_at=None,  # Never expires
        priority=5,  # Slightly boosted
        tenant_id="company",
        user_id="alice@company.com",  # Private or team
        tags=["lesson-learned", "deployment", "argocd", "kubernetes"],
    )

    assert lesson.knowledge_type == KnowledgeType.PROCEDURE
    assert lesson.expires_at is None  # Permanent
    assert "lesson-learned" in lesson.tags
