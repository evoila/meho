# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approver routing in the web interface (``POLICY_APPROVER_ROUTING``).

* The request window and the bell list say who may approve.
* A person outside the stored group kinds sees why, and Approve is off.
* The "For you" tab lists only the requests the person may approve. It
  exists only while the switch is on.
* The bell counts only the requests the person may approve.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import respx
from sqlalchemy import update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.settings import get_settings
from tests.test_ui_approvals import (
    _HX_HEADERS,
    _RESOLVE_OPERATOR,
    _TENANT_A,
    _authenticated_client,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _operator,
    _seed_request,
    _seed_session_sync,
    _seed_tenant,
)

_WHO = "Members of MEHO-SA-Operators or MEHO-Admins."


@pytest.fixture
def _routing_on(monkeypatch: pytest.MonkeyPatch, _bff_env: None) -> Iterator[None]:  # noqa: F811
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _routed(op_id: str, kinds: list[str] | None) -> uuid.UUID:
    """A pending caution request of another person, with stored group kinds."""
    rid = _seed_request(
        tenant_id=_TENANT_A, op_id=op_id, proposed_effect={"safety_level": "caution"}
    )

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == rid)
                .values(approver_roles=kinds, approver_rule="install:routing[0]")
            )

    asyncio.run(_do())
    return rid


def _get(url: str, operator: Operator, *, htmx: bool = False) -> tuple[int, str]:
    session_id = _seed_session_sync(tenant_id=_TENANT_A, operator_sub=operator.sub)
    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new_callable=AsyncMock, return_value=operator):
            response = client.get(url, headers=_HX_HEADERS if htmx else None)
    return response.status_code, response.text


def _sa_operator() -> Operator:
    return _operator(tenant_id=_TENANT_A).model_copy(update={"self_approver": True})


@pytest.mark.usefixtures("_routing_on")
def test_the_window_says_who_may_approve_and_refuses_another_group() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _routed("demo.network.change", ["SA-Operators", "Admins"])

    code, body = _get(f"/ui/approvals/{rid}", _operator(tenant_id=_TENANT_A))

    assert code == 200, body
    assert "data-approval-who-can-approve" in body
    assert _WHO in body
    assert 'data-approve-reason="not_routed"' in body
    assert "Only MEHO-SA-Operators or MEHO-Admins may approve this change" in body


@pytest.mark.usefixtures("_routing_on")
def test_a_person_in_the_stored_kinds_may_approve_in_the_window() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _routed("demo.network.change", ["SA-Operators", "Admins"])

    code, body = _get(f"/ui/approvals/{rid}", _sa_operator())

    assert code == 200, body
    assert 'data-approve-right="yes"' in body


def test_with_the_switch_off_the_window_shows_no_routing() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    rid = _routed("demo.network.change", ["SA-Operators", "Admins"])

    code, body = _get(f"/ui/approvals/{rid}", _operator(tenant_id=_TENANT_A))

    assert code == 200, body
    assert "data-approval-who-can-approve" not in body
    assert 'data-approve-right="yes"' in body


@pytest.mark.usefixtures("_routing_on")
def test_the_bell_list_says_who_may_approve() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _routed("demo.network.change", ["SA-Operators", "Admins"])

    code, body = _get("/ui/approvals", _operator(tenant_id=_TENANT_A), htmx=True)

    assert code == 200, body
    assert f"Who can approve: {_WHO}" in " ".join(body.split())


@pytest.mark.usefixtures("_routing_on")
def test_the_for_you_tab_lists_only_what_the_person_may_approve() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _routed("demo.network.change", ["SA-Operators", "Admins"])
    _routed("demo.compute.change", ["Operators", "SA-Operators", "Admins", "Approvers"])

    code, body = _get("/ui/approvals/list?tab=for_you", _operator(tenant_id=_TENANT_A), htmx=True)

    assert code == 200, body
    assert "demo.compute.change" in body
    assert "demo.network.change" not in body
    assert "For you" in body


@pytest.mark.usefixtures("_routing_on")
def test_the_for_you_tab_never_shows_an_empty_list_when_the_person_is_unknown() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _routed("demo.compute.change", ["Operators"])
    session_id = _seed_session_sync(tenant_id=_TENANT_A)
    failure = AsyncMock(side_effect=RuntimeError("sign-in service down"))

    with respx.mock(assert_all_called=False):
        client = _authenticated_client(session_id)
        with patch(_RESOLVE_OPERATOR, new=failure):
            response = client.get("/ui/approvals/list?tab=for_you", headers=_HX_HEADERS)

    assert response.status_code == 503
    assert "cannot list the requests for you" in response.text


def test_with_the_switch_off_there_is_no_for_you_tab() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")

    code, _body = _get("/ui/approvals/list?tab=for_you", _operator(tenant_id=_TENANT_A), htmx=True)
    pending_code, pending = _get(
        "/ui/approvals/list?tab=pending", _operator(tenant_id=_TENANT_A), htmx=True
    )

    assert code == 422
    assert pending_code == 200
    assert "For you" not in pending


@pytest.mark.usefixtures("_routing_on")
def test_the_bell_counts_only_what_the_person_may_approve() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _routed("demo.network.change", ["SA-Operators", "Admins"])
    _routed("demo.compute.change", ["Operators"])

    code, body = _get("/ui/approvals/badge", _operator(tenant_id=_TENANT_A))

    assert code == 200, body
    assert 'data-pending-count="1"' in body
    assert "for you" in body


@pytest.mark.usefixtures("_routing_on")
def test_the_bell_counts_a_request_for_the_person_behind_fifty_newer_ones() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    now = datetime.now(UTC)
    _seed_request(
        tenant_id=_TENANT_A,
        op_id="demo.compute.change",
        proposed_effect={"safety_level": "caution"},
        created_at=now - timedelta(hours=2),
    )
    for minute in range(50):
        _seed_request(
            tenant_id=_TENANT_A,
            op_id="demo.network.change",
            proposed_effect={"safety_level": "caution"},
            created_at=now - timedelta(minutes=minute),
        )

    async def _route() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for op_id, kinds in (
                ("demo.compute.change", ["Operators"]),
                ("demo.network.change", ["SA-Operators", "Admins"]),
            ):
                await session.execute(
                    update(ApprovalRequest)
                    .where(ApprovalRequest.op_id == op_id)
                    .values(approver_roles=kinds, approver_rule="install:routing[0]")
                )

    asyncio.run(_route())

    # Small batches, so the count also reads past the first batch.
    with patch("meho_backplane.ui.routes.approvals.routes._WAITING_SCAN_BATCH", 20):
        code, body = _get("/ui/approvals/badge", _operator(tenant_id=_TENANT_A))

    assert code == 200, body
    assert 'data-pending-count="1"' in body


def test_with_the_switch_off_the_bell_counts_every_pending_request() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _routed("demo.network.change", ["SA-Operators", "Admins"])
    _routed("demo.compute.change", ["Operators"])

    code, body = _get(
        "/ui/approvals/badge", _operator(tenant_id=_TENANT_A, role=TenantRole.OPERATOR)
    )

    assert code == 200, body
    assert 'data-pending-count="2"' in body
