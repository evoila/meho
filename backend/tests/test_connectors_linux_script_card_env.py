# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of ``linux.script.run`` shows each ``env`` value.

* A waiting request holds each ``env`` name with its value, and one line that
  secrets belong in ``secret_env``.
* ``secret_env`` shows only its references, never a value.
* The console approval window shows the ``env`` values, a control
  character as a visible escape. The MCP approval reads get the ``env``
  names only, also for a request that another principal sent.
* The REST approval list and detail give a person's login the ``env``
  values. An agent or service login gets the ``env`` names only.
* The params hash binds the ``env`` values: an approval with another value
  is refused.
* The script gets each ``env`` value byte for byte as the card shows it,
  also with line breaks at the end.
* The agent's own ``preview_operation`` answer has the ``env`` names only.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.linux import LinuxSshConnector
from meho_backplane.connectors.linux.ops_write import ENV_NOTE, build_script_run_wrapper
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


def _rest_reader(kind: PrincipalKind) -> Operator:
    return Operator(
        sub=f"linux-env-card-rest-{kind.value}",
        name=None,
        email=None,
        raw_jwt="<linux-env-card-rest-raw-jwt>",
        tenant_id=_OPERATOR.tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _rest_reads(operator: Operator, request_id: uuid.UUID) -> list[dict[str, Any]]:
    from meho_backplane.api.v1.approvals import get_approval_request, list_approvals

    detail = await get_approval_request(request_id, operator=operator)
    listed = await list_approvals(
        status="pending",
        work_ref=None,
        limit=None,
        offset=0,
        order="newest",
        outcome_unknown=False,
        operator=operator,
    )
    rows = [row for row in listed if row.id == request_id]
    assert len(rows) == 1
    return [detail.proposed_effect, rows[0].proposed_effect]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_rest_approval_read_by_an_agent_or_service_gets_the_env_names_only(
    linux_write_registered: None,  # noqa: F811 -- fixture
    kind: PrincipalKind,
) -> None:
    request_id = await _park({"script": _SCRIPT, "env": {"TAR_OPTIONS": "env-card-value"}})
    for effect in await _rest_reads(_rest_reader(kind), request_id):
        preview = effect["preview"]
        assert preview["env_var_names"] == ["TAR_OPTIONS"]
        assert "env" not in preview
        assert "env_note" not in preview
        assert "env-card-value" not in repr(effect)


@pytest.mark.asyncio
async def test_rest_approval_read_by_a_person_gets_the_env_values(
    linux_write_registered: None,  # noqa: F811 -- fixture
) -> None:
    request_id = await _park({"script": _SCRIPT, "env": {"TAR_OPTIONS": "env-card-value"}})
    for effect in await _rest_reads(_rest_reader(PrincipalKind.USER), request_id):
        assert effect["preview"]["env"] == {"TAR_OPTIONS": "env-card-value"}
        assert effect["preview"]["env_note"] == ENV_NOTE


_PRINT_ENV_SCRIPT = "import json, os, sys\nsys.stdout.write(json.dumps(os.environ['APP_CONFIG']))\n"


def _value_the_script_gets(env: dict[str, str]) -> str:
    """Run the real wrapper with bash; the script prints the ``APP_CONFIG`` it got."""
    wrapper = build_script_run_wrapper(
        sys.executable, _PRINT_ENV_SCRIPT, arguments=None, working_directory=None, env=env
    )
    proc = subprocess.run(
        ["bash", "-c", wrapper], capture_output=True, text=True, timeout=30, check=True
    )
    value = json.loads(proc.stdout)
    assert isinstance(value, str)
    return value


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "line one\nline two\n",
        "line one\n\n\n",
        "\n",
        "\n\n\n",
        "",
        "line one\nline two",
        "ends in x",
        "ends in x\n",
    ],
    ids=[
        "one-break-at-end",
        "several-breaks-at-end",
        "only-one-break",
        "only-breaks",
        "empty",
        "break-in-middle",
        "ends-in-x",
        "ends-in-x-and-break",
    ],
)
async def test_the_script_gets_the_env_value_on_the_card_byte_for_byte(
    linux_write_registered: None,  # noqa: F811 -- fixture
    value: str,
) -> None:
    env = {"APP_CONFIG": value}
    request_id = await _park({"script": _SCRIPT, "env": env})
    card_value = (await _proposed_effect(request_id))["preview"]["env"]["APP_CONFIG"]
    assert card_value == value
    assert await asyncio.to_thread(_value_the_script_gets, env) == card_value


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_preview", [True, False], ids=["agent", "person"])
async def test_agent_preview_has_the_env_names_only(agent_preview: bool) -> None:
    from meho_backplane.connectors.linux.ops_write import _linux_script_run_preview

    ctx = SimpleNamespace(
        params={"script": _SCRIPT, "env": {"TAR_OPTIONS": "env-card-value"}},
        target=SimpleNamespace(name="linux-1"),
        agent_preview=agent_preview,
    )
    preview = await _linux_script_run_preview(ctx)  # type: ignore[arg-type]
    assert preview is not None
    assert preview["env_var_names"] == ["TAR_OPTIONS"]
    if agent_preview:
        assert "env" not in preview
        assert "env_note" not in preview
        assert "env-card-value" not in repr(preview)
    else:
        assert preview["env"] == {"TAR_OPTIONS": "env-card-value"}
        assert preview["env_note"] == ENV_NOTE
