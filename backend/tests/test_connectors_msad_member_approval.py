# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The msad group membership writes wait for a person to approve them.

``msad.group.add-member`` and ``msad.group.remove-member`` are ``caution`` and
``requires_approval``. Every test goes through ``dispatch()`` with the real
stored operations:

* a person in the web interface waits for approval, and nothing runs on the
  domain controller;
* a person with a token and an agent wait too;
* a ``caution`` write without the approval flag (``msad.ou.create``) still runs
  at once in the web interface, so the wait comes from the flag.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.msad import MsadConnector, register_msad_typed_operations
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "msad-ssh-2022.x"
_MEMBER_OPS = ("msad.group.add-member", "msad.group.remove-member")
_MEMBER_PARAMS = {"identity": "Domain Admins", "members": ["jdoe"]}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(product="msad", version="2022.x", impl_id="msad-ssh", cls=MsadConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


class _Target:
    """A target with the fields the resolver and the connector read."""

    def __init__(self) -> None:
        self.product = "msad"
        self.fingerprint = type("_FP", (), {"version": "2022"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = f"msad-{self.id.hex[:6]}"
        self.host = "dc.test.invalid"
        self.port = 22
        self.secret_ref = "targets/test/msad"
        self.auth_model = "shared_service_account"


def _operator(*, web_session: bool = False, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=f"op-msad-{kind.value}",
        name=None,
        email=None,
        raw_jwt="op.msad.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        web_session=web_session,
    )


def _completed(payload: dict[str, Any]) -> Any:
    proc = MagicMock()
    proc.stdout = json.dumps(payload)
    proc.stderr = ""
    proc.exit_status = 0
    return proc


async def _run(op_id: str, params: dict[str, Any], operator: Operator) -> tuple[Any, AsyncMock]:
    await register_msad_typed_operations()
    connector = get_or_create_connector_instance(MsadConnector)
    run_mock = AsyncMock(return_value=_completed({"ok": True}))
    with patch.object(connector, "_run_command", run_mock):
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR_ID,
            op_id=op_id,
            target=_Target(),
            params=params,
        )
    return result, run_mock


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _MEMBER_OPS)
async def test_web_console_member_write_waits_for_approval(op_id: str) -> None:
    result, run_mock = await _run(op_id, dict(_MEMBER_PARAMS), _operator(web_session=True))

    assert result.status == "awaiting_approval", result.error
    run_mock.assert_not_awaited()
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
    assert request is not None
    assert request.op_id == op_id


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", _MEMBER_OPS)
@pytest.mark.parametrize(
    "operator",
    [_operator(), _operator(kind=PrincipalKind.AGENT)],
    ids=["person-token", "agent"],
)
async def test_token_and_agent_member_write_waits_for_approval(
    op_id: str, operator: Operator
) -> None:
    result, run_mock = await _run(op_id, dict(_MEMBER_PARAMS), operator)

    assert result.status == "awaiting_approval", result.error
    run_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_console_caution_write_without_the_flag_runs_at_once() -> None:
    result, run_mock = await _run(
        "msad.ou.create",
        {"name": "Servers", "path": "DC=example,DC=test"},
        _operator(web_session=True),
    )

    assert result.status == "ok", result.error
    run_mock.assert_awaited_once()
