# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval surfaces mark a machine requester (#3885).

An approval request saves the requester's ``principal_kind``. REST
(``ApprovalRequestView.principal_kind``) returns it, and the console shows
a badge with the kind next to the requester when an agent, a service
account or a runner asked. A person, or a request saved before MEHO kept
the kind, gets no badge.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import respx
from sqlalchemy import update

from meho_backplane.api.v1.approvals import _view
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.ui.references import subject_ref
from tests.test_ui_approvals import (
    _HX_HEADERS,
    _RESOLVE_OPERATOR,
    _REVIEWER_SUB,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_BADGE = 'data-subject-kind="agent"'


@pytest.fixture(autouse=True)
def _tenant() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")


def _seed(*, name: str | None, kind: str | None) -> uuid.UUID:
    rid = _seed_request(tenant_id=_TENANT_A)

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == rid)
                .values(principal_name=name, principal_kind=kind)
            )

    asyncio.run(_do())
    return rid


def _load(request_id: uuid.UUID) -> ApprovalRequest:
    async def _do() -> ApprovalRequest:
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            return row

    return asyncio.run(_do())


def _modal(request_id: uuid.UUID) -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    operator = _operator(tenant_id=_TENANT_A, sub=_REVIEWER_SUB)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(f"/ui/approvals/{request_id}")
    assert response.status_code == 200, response.text
    return str(response.text)


def _panel() -> str:
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        response = client.get("/ui/approvals", headers=_HX_HEADERS)
    assert response.status_code == 200, response.text
    return str(response.text)


def test_rest_view_returns_the_requester_kind() -> None:
    assert _view(_load(_seed(name="incident-triage", kind="agent"))).principal_kind == "agent"
    assert _view(_load(_seed(name=None, kind=None))).principal_kind is None


def test_subject_ref_marks_only_machine_kinds() -> None:
    for kind in ("agent", "service", "runner"):
        ref = subject_ref("sa-sub", "x", kind)
        assert ref is not None
        assert ref.machine_kind == kind
    for kind in ("user", None, "", "robot"):
        ref = subject_ref("sa-sub", "x", kind)
        assert ref is not None
        assert ref.machine_kind is None


def test_modal_and_panel_mark_an_agent_requester() -> None:
    rid = _seed(name="incident-triage", kind="agent")

    modal = _modal(rid)
    panel = _panel()

    for body in (modal, panel):
        assert "incident-triage" in body
        assert _BADGE in body


def test_person_and_old_requests_get_no_badge() -> None:
    person = _seed(name="Bob Requester", kind="user")
    old = _seed(name=None, kind=None)

    assert "data-subject-kind" not in _modal(person)
    assert "data-subject-kind" not in _modal(old)
    assert "data-subject-kind" not in _panel()
