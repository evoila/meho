# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A blank Target field in the console's Run window sends no target (#5705).

The Run window says "leave blank for a target-less op". These tests check
that, with the field blank or only spaces:

* an op that needs no target runs (and previews), with no target;
* an op that needs a target still answers ``target_required`` as the
  inline form error, and nothing is sent to the vendor.

They run the real ``call_operation`` / ``preview_operation`` in-process,
not a stand-in, so the dispatcher's own target check is what answers.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from tests.test_ui_operations import (
    _OP_ADMIN,
    _OP_OPERATOR,
    _TENANT_A,
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _client_with_role_and_csrf,
    _csrf_headers,
    _register_recording_gh_connector,
    _seed_ingested_op,
    _seed_tenant,
)

_TARGETLESS_OP = "demo.ping"

#: The target value each run of :func:`_targetless_handler` received.
_SEEN_TARGETS: list[Any] = []


async def _targetless_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A module-level typed handler: it needs no target."""
    _SEEN_TARGETS.append(target)
    return {"pong": True}


def _seed_targetless_op() -> None:
    """Seed one enabled typed op whose handler needs no target."""

    async def _do() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session, session.begin():
            session.add(
                EndpointDescriptor(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product="demo",
                    version="1",
                    impl_id="demo",
                    op_id=_TARGETLESS_OP,
                    source_kind="typed",
                    method=None,
                    path=None,
                    handler_ref="tests.test_ui_operations_blank_target._targetless_handler",
                    summary="Ping.",
                    description="A target-less test op.",
                    parameter_schema={"type": "object"},
                    llm_instructions=None,
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    asyncio.run(_do())


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize(
    ("sub", "role"),
    [(_OP_OPERATOR, TenantRole.OPERATOR), (_OP_ADMIN, TenantRole.TENANT_ADMIN)],
)
def test_run_blank_target_runs_targetless_op(blank: str, sub: str, role: TenantRole) -> None:
    """A target-less op runs from the Run window with the Target field blank."""
    _SEEN_TARGETS.clear()
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_targetless_op()
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=sub, role=role
    )
    with mock:
        response = client.post(
            "/ui/operations/call",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "demo-1",
                "op_id": _TARGETLESS_OP,
                "target": blank,
                "params": "{}",
            },
        )
    assert response.status_code == 200, response.text
    assert 'data-run-status="ok"' in response.text
    assert "target_required" not in response.text
    # The handler ran once, with no target.
    assert _SEEN_TARGETS == [None]


@pytest.mark.parametrize("blank", ["", "   "])
def test_preview_blank_target_previews_targetless_op(blank: str) -> None:
    """The Preview button with a blank Target field also sends no target."""
    _SEEN_TARGETS.clear()
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_targetless_op()
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/preview",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "demo-1",
                "op_id": _TARGETLESS_OP,
                "target": blank,
                "params": "{}",
            },
        )
    assert response.status_code == 200, response.text
    assert 'data-preview-status="form_error"' not in response.text
    assert "target_required" not in response.text
    # A preview never runs the op.
    assert _SEEN_TARGETS == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_run_blank_target_on_op_that_needs_one_is_target_required(blank: str) -> None:
    """An op that needs a target still answers ``target_required`` with a blank field."""
    _seed_tenant(_TENANT_A, "tenant-a")
    connector = _register_recording_gh_connector()
    _seed_ingested_op(tenant_id=None)
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_OP_OPERATOR, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/call",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "gh-rest-3",
                "op_id": "POST:/repos/{owner}/{repo}/issues",
                "target": blank,
                "params": '{"owner": "example", "repo": "demo"}',
            },
        )
    assert response.status_code == 400, response.text
    assert 'data-run-status="form_error"' in response.text
    assert "needs a target" in response.text
    # Nothing was sent to the vendor.
    assert connector.calls == []
