# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route contracts for the REST adapters behind `meho broadcast` (#3470)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.broadcast import router
from meho_backplane.auth.agent_admission import AgentIdentityRequiredError
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import verify_jwt_and_bind
from meho_backplane.broadcast import AgentAnnouncementEvent, AnnouncementPublishResult
from tests.broadcast_admission_helpers import fake_admission

_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")


def _operator() -> Operator:
    return Operator(
        sub="operator-a",
        name="Operator A",
        email=None,
        raw_jwt="test-jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_jwt_and_bind] = _operator
    return TestClient(app)


def test_recent_uses_shared_strict_history_reader() -> None:
    expected = {"events": [{"cursor": "1-0", "kind": "announcement"}], "next_cursor": "1-0"}
    with patch(
        "meho_backplane.api.v1.broadcast.list_recent_events_strict",
        new=AsyncMock(return_value=expected),
    ) as recent:
        response = _client().get(
            "/api/v1/broadcast/recent",
            params={"cursor": "2026-09-13T12:00:00Z", "target": "cluster-a", "limit": 5},
        )

    assert response.status_code == 200
    assert response.json() == expected
    assert recent.await_args.kwargs == {
        "since": "2026-09-13T12:00:00Z",
        "op_class": None,
        "principal": None,
        "target": "cluster-a",
        "actor_sub": None,
        "work_ref": None,
        "active_only": False,
        "limit": 5,
    }
    assert recent.await_args.args[0].tenant_id == _TENANT_ID


async def _published(event: AgentAnnouncementEvent, **_kwargs: Any) -> AnnouncementPublishResult:
    return AnnouncementPublishResult(event=event, cursor="1726228800000-0", replayed=False)


def test_announce_uses_rate_limit_and_shared_durable_publisher() -> None:
    with (
        patch(
            "meho_backplane.api.v1.broadcast.resolve_agent_admission",
            new=AsyncMock(side_effect=lambda op: fake_admission(op)),
        ),
        patch(
            "meho_backplane.api.v1.broadcast.enforce_announce_rate_limit",
            new=AsyncMock(),
        ) as rate_limit,
        patch(
            "meho_backplane.api.v1.broadcast.publish_agent_announcement",
            new=AsyncMock(side_effect=_published),
        ) as publish,
    ):
        response = _client().post(
            "/api/v1/broadcast/announce",
            json={
                "activity": "Investigating cluster-a",
                "target": "cluster-a",
                "phase": "start",
                "ttl_minutes": 30,
                "work_ref": "gh:evoila/meho#3470",
            },
        )

    assert response.status_code == 201
    assert response.json()["cursor"] == "1726228800000-0"
    rate_limit.assert_awaited_once_with(_TENANT_ID, "operator-a")
    event = publish.await_args.args[0]
    assert event.tenant_id == _TENANT_ID
    assert event.principal_sub == "operator-a"
    assert event.activity == "Investigating cluster-a"
    assert event.work_ref == "gh:evoila/meho#3470"
    assert event.owner_sub == "agent:operator-a"
    assert response.json()["claim_id"] == str(event.claim_id)


def test_announce_from_a_person_returns_agent_identity_required() -> None:
    """#3736: a person's own token cannot publish; the error says how to fix it."""
    with (
        patch(
            "meho_backplane.api.v1.broadcast.resolve_agent_admission",
            new=AsyncMock(side_effect=AgentIdentityRequiredError("a user token")),
        ),
        patch(
            "meho_backplane.api.v1.broadcast.publish_agent_announcement",
            new=AsyncMock(),
        ) as publish,
    ):
        response = _client().post("/api/v1/broadcast/announce", json={"activity": "hello"})

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "agent_identity_required"
    assert "meho agent-principal register" in detail["remediation"]
    publish.assert_not_awaited()


def test_recent_rejects_unknown_op_class_before_reading_history() -> None:
    with patch(
        "meho_backplane.api.v1.broadcast.list_recent_events_strict",
        new=AsyncMock(),
    ) as recent:
        response = _client().get("/api/v1/broadcast/recent", params={"op_class": "unknown"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_op_class"
    recent.assert_not_awaited()
