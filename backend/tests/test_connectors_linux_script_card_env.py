# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``linux.script.run`` shows each ``env`` value.

* A waiting request holds each ``env`` name with its value, and one line that
  secrets belong in ``secret_env``.
* ``secret_env`` shows only its references, never a value.
* The console approval window shows the ``env`` values, a control
  character as a visible escape. The MCP approval reads get the ``env``
  names only, also for a request that another principal sent.
* The params hash binds the ``env`` values: an approval with another value
  is refused.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import ENV_NOTE
from meho_backplane.operations._preview import proposed_effect_for_agent
from tests.test_connectors_linux_write import (
    _CONNECTOR_ID,
    _OPERATOR,
    _SECRET_ENV_REF,
    _SECRET_ENV_VALUE_CANARY,
    _TARGET_NAME,
    _required_settings_env,  # noqa: F401 -- autouse fixture
    linux_write_registered,  # noqa: F401 -- fixture
)
from tests.test_ui_approvals import (
    _bff_env,  # noqa: F401 -- autouse fixture: pins the BFF env for these tests
    _render_modal,
)

_SCRIPT = "#!/bin/bash\nset -eu\ntar -cf /tmp/out.tar /etc/app\n"
_REVIEWER = Operator(
    sub="linux-env-card-reviewer",
    name="Linux Env Card Reviewer",
    email=None,
    raw_jwt="<linux-env-card-reviewer-raw-jwt>",
    tenant_id=_OPERATOR.tenant_id,
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _park(params: dict[str, Any]) -> uuid.UUID:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.operations import dispatch
    from meho_backplane.targets.resolver import resolve_target

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=_CONNECTOR_ID,
        op_id="linux.script.run",
        target=target,
        params=params,
        _approved=False,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval", dumped
    return uuid.UUID(dumped["extras"]["approval_request_id"])


async def _proposed_effect(request_id: uuid.UUID) -> dict[str, Any]:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequest

    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert isinstance(row.proposed_effect, dict)
    return row.proposed_effect


@pytest.mark.asyncio
async def test_waiting_request_card_shows_the_env_value(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    request_id = await _park({"script": _SCRIPT, "env": {"TAR_OPTIONS": "x", "APP_MODE": "on"}})
    preview = (await _proposed_effect(request_id))["preview"]
    assert preview["env"] == {"APP_MODE": "on", "TAR_OPTIONS": "x"}
    assert preview["env_var_names"] == ["APP_MODE", "TAR_OPTIONS"]
    assert preview["env_note"] == ENV_NOTE


@pytest.mark.asyncio
async def test_waiting_request_card_shows_only_the_secret_env_reference(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    resolve = AsyncMock(return_value={"cred": _SECRET_ENV_VALUE_CANARY})
    with patch.object(LinuxSshConnector, "_resolve_secret", resolve):
        request_id = await _park(
            {
                "script": _SCRIPT,
                "env": {"APP_MODE": "on"},
                "secret_env": {"APP_DB_CRED": _SECRET_ENV_REF},
            }
        )
    effect = await _proposed_effect(request_id)
    preview = effect["preview"]
    assert preview["secret_env"] == {"APP_DB_CRED": _SECRET_ENV_REF}
    assert preview["env"] == {"APP_MODE": "on"}
    assert "APP_DB_CRED" not in preview["env"]
    resolve.assert_not_awaited()
    assert _SECRET_ENV_VALUE_CANARY not in repr(effect)


@pytest.mark.asyncio
async def test_approval_with_another_env_value_is_refused(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import ApprovalRequestStatus
    from meho_backplane.operations.approval_queue import ParamsMismatchError, approve_request

    params = {"script": _SCRIPT, "env": {"TAR_OPTIONS": "x"}}
    request_id = await _park(dict(params))
    async with get_sessionmaker()() as session:
        with pytest.raises(ParamsMismatchError):
            await approve_request(
                session,
                request_id,
                operator=_REVIEWER,
                params={**params, "env": {"TAR_OPTIONS": "y"}},
            )
    async with get_sessionmaker()() as session:
        approved = await approve_request(session, request_id, operator=_REVIEWER, params=params)
        await session.commit()
    assert approved.status == ApprovalRequestStatus.APPROVED.value


def _effect(env: dict[str, str]) -> dict[str, Any]:
    return {
        "op_id": "linux.script.run",
        "connector_id": "linux-ssh",
        "op_class": "credential_write",
        "preview": {
            "target": "linux-1",
            "interpreter": "/bin/bash",
            "script": _SCRIPT,
            "script_bytes": len(_SCRIPT),
            "env_var_names": sorted(env),
            "env": env,
            "env_note": ENV_NOTE,
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }


def test_console_window_shows_env_values_with_control_characters_escaped() -> None:
    body = _render_modal(_effect({"TAR_OPTIONS": "x\x1b[2Ky", "APP_MODE": "on"}))
    table = body.split('aria-label="Proposed effect fields"')[1].split("</table>")[0]
    assert "TAR_OPTIONS" in table
    assert "x\\u001b[2Ky" in table
    assert "\x1b" not in body
    assert ENV_NOTE in table


def test_mcp_approval_read_gets_the_env_names_only() -> None:
    effect = _effect({"TAR_OPTIONS": "x"})
    shown = proposed_effect_for_agent(
        "linux.script.run", effect, params=None, connector_id="linux-ssh", tenant=None
    )
    assert shown is not None
    assert "env" not in shown["preview"]
    assert "env_note" not in shown["preview"]
    assert shown["preview"]["env_var_names"] == ["TAR_OPTIONS"]
    assert shown["preview"]["script"] == _SCRIPT
    assert effect["preview"]["env"] == {"TAR_OPTIONS": "x"}


@pytest.mark.asyncio
async def test_stored_request_shows_env_values_in_the_console_and_names_only_over_mcp(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler

    request_id = await _park({"script": _SCRIPT, "env": {"TAR_OPTIONS": "env-card-value"}})
    effect = await _proposed_effect(request_id)

    # The console seeds its own copy of the stored card (it runs its own loop).
    body = await asyncio.to_thread(_render_modal, effect)
    assert "env-card-value" in body
    assert ENV_NOTE in body

    # _REVIEWER is another principal than the one that sent the request.
    got = await _get_handler(_REVIEWER, {"approval_request_id": str(request_id)})
    listed = await _list_handler(_REVIEWER, {"status": "pending"})
    rows = [row for row in listed["items"] if row["id"] == str(request_id)]
    assert len(rows) == 1
    for row in (got, rows[0]):
        preview = row["proposed_effect"]["preview"]
        assert preview["env_var_names"] == ["TAR_OPTIONS"]
        assert "env" not in preview
        assert "env_note" not in preview
        assert "env-card-value" not in repr(row)
