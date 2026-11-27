# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A person in the web console with the Operator role keeps every answer.

The console twin of ``test_reader_change_controls.py``. Each case runs one
kind of change through one way in: the real ``dispatch`` with a console
login, and the console call form ``POST /ui/operations/call``. The kinds,
with the risk level and the approval flag of each stand-in, are the table
``KINDS`` of that file. It checks
the status, the code, the number of stored approval requests and whether
the stand-in ran. The expected values are the ones the same cases give on
a MEHO without the Reader rule.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Target
from meho_backplane.operations import dispatch
from tests._people_behind_helpers import give_known_person
from tests.test_agent_grant_required import _TENANT, stub_embedding_service
from tests.test_reader_change_controls import KINDS, RUNS, register_stand_ins
from tests.test_ui_operations import (
    _bff_env,
    _client_with_role_and_csrf,
    _csrf_headers,
    _seed_tenant,
    _wait_for_the_answer,
)

__all__ = ["_bff_env", "_wait_for_the_answer", "stub_embedding_service"]

_SUB = "console-person-1"
_WAITS = ("awaiting_approval", "awaiting_approval", 1, False)
_RUNS_AT_ONCE = ("ok", None, 0, True)
_NO_PREVIEW = ("denied", "preview_binding_required", 0, False)

#: (way, kind) -> (status, code, stored approval requests, ran). These are
#: the answers without the Reader rule.
_PER_KIND: dict[str, tuple[str, str | None, int, bool]] = {
    "write_safe": _RUNS_AT_ONCE,
    "write_caution": _RUNS_AT_ONCE,
    "write_dangerous": _WAITS,
    "rights_change": _WAITS,
    "multi_step": _RUNS_AT_ONCE,
    "delete_no_preview": _NO_PREVIEW,
}
EXPECTED: dict[tuple[str, str], tuple[str, str | None, int, bool]] = {
    (way, kind): outcome
    for way in ("dispatch", "console_form")
    for kind, outcome in _PER_KIND.items()
}


def _set_up(embedding: Any) -> None:
    """The stand-in operations, the tenant and the target they run on."""

    async def _do() -> None:
        RUNS.clear()
        await register_stand_ins(embedding, module="tests.test_reader_change_controls")

    _seed_tenant(_TENANT, "console-controls")
    asyncio.run(_do())


def _console_person() -> Operator:
    return Operator(
        sub=_SUB,
        name="Person",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        web_session=True,
    )


def _approval_requests() -> int:
    async def _count() -> int:
        async with get_sessionmaker()() as session:
            query = select(func.count()).select_from(ApprovalRequest)
            return int((await session.execute(query)).scalar_one())

    return asyncio.run(_count())


def _via_dispatch(op_id: str) -> tuple[str, str | None]:
    async def _do() -> tuple[str, str | None]:
        async with get_sessionmaker()() as session:
            target = (
                await session.execute(select(Target).where(Target.name == "vault-grant-test"))
            ).scalar_one()
            session.expunge(target)
        result = await dispatch(
            operator=_console_person(),
            connector_id="vault-1.x",
            op_id=op_id,
            target=target,
            params={"path": "app/config"},
        )
        code = result.extras.get("error_code") or result.extras.get("reason_code")
        return result.status, str(code) if code else None

    return asyncio.run(_do())


_STATUS = re.compile(r'data-run-status="([a-z_]+)"')
_CODE = re.compile(r"data-run-error-code>([^<]+)<")


def _via_form(op_id: str) -> tuple[str, str | None]:
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT, operator_sub=_SUB, role=TenantRole.OPERATOR
    )
    with mock:
        response = client.post(
            "/ui/operations/call",
            headers=_csrf_headers(csrf),
            data={
                "connector_id": "vault-1.x",
                "op_id": op_id,
                "target": "vault-grant-test",
                "params": '{"path": "app/config"}',
            },
        )
    assert response.status_code == 200, response.text
    status = _STATUS.search(response.text)
    code = _CODE.search(response.text)
    assert status is not None, response.text
    found = code.group(1).strip() if code else None
    if found is None and status.group(1) == "awaiting_approval":
        found = "awaiting_approval"
    return status.group(1), found


@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("way", ["dispatch", "console_form"])
@pytest.mark.usefixtures("_wait_for_the_answer")
def test_an_operator_in_the_console_keeps_its_answer(
    stub_embedding_service: Any, way: str, kind: str
) -> None:
    _set_up(stub_embedding_service)
    asyncio.run(give_known_person(_console_person()))
    op_id = KINDS[kind][0]

    if way == "dispatch":
        status, code = _via_dispatch(op_id)
    else:
        status, code = _via_form(op_id)

    outcome = (status, code, _approval_requests(), bool(RUNS))
    assert outcome == EXPECTED[(way, kind)], outcome
