# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Case D: the approval card holds the login-realm fact, so ``own_approval`` is ``null``.

``keycloak.authflow.ensure_approval_flow`` (the real registration) on a
fake Keycloak, asked by a tenant admin who is not a platform admin, with a
token. Explain asks without the call's values and without the card, so
``web`` and ``token`` are ``null`` with the reason. The real call stores
the card: outside a protected login realm it records ``login_realm=false``
and the admin approves it in the web console; in a protected login realm
it records ``true`` and the admin does not approve it.

Fixture values are made up.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations.approval_queue import ApprovalError, approve_request
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target
from tests.test_approval_flow_op_who_runs_it import runs  # noqa: F401 -- fixture: fake Keycloak
from tests.test_rights_change_console_run import (
    _CONNECTOR_ID,
    _TARGET_NAME,
    _TENANT_ID,
    _required_settings_env,  # noqa: F401 -- autouse fixture: the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)

from ._own_approval_cases import DEPENDS, call

_OP = "keycloak.authflow.ensure_approval_flow"
_DEMO_TARGET = "demo-keycloak"


@pytest.fixture(autouse=True)
def _custom_policy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_demo_target() -> None:
    """A Keycloak target whose realm is not a protected login realm."""
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_DEMO_TARGET,
                aliases=[],
                product="keycloak",
                host="demo-keycloak.test.invalid",
                port=443,
                fqdn=None,
                secret_ref="example-site/keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": "demo-realm"},
                fingerprint={"version": "26.0.5"},
                notes="seeded by test_policy_explain_own_approval_card",
            )
        )
        await session.commit()


def _tenant_admin(*, web: bool) -> Operator:
    return Operator(
        sub="demo-tenant-admin",
        raw_jwt="<demo-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=False,
        web_session=web,
        acr="1",
        auth_time=int(time.time()) - 5,
    )


async def _approve(request_id: uuid.UUID, *, web: bool, params: dict[str, Any]) -> bool:
    async with get_sessionmaker()() as session:
        try:
            await approve_request(
                session, request_id, operator=_tenant_admin(web=web), params=params
            )
        except ApprovalError:
            return False
        finally:
            await session.rollback()
    return True


@pytest.mark.asyncio
@pytest.mark.usefixtures("runs")
@pytest.mark.parametrize(
    "params", [{}, {"client_id": "demo-cli-approve"}], ids=["no-values", "client"]
)
@pytest.mark.parametrize(
    ("target_name", "login_realm", "approve"),
    [
        pytest.param(_DEMO_TARGET, False, (True, False), id="demo-realm"),
        pytest.param(_TARGET_NAME, True, (False, False), id="protected-login-realm"),
    ],
)
async def test_case_d_the_part_is_null_and_the_card_holds_the_login_realm(
    params: dict[str, Any],
    target_name: str,
    login_realm: bool,
    approve: tuple[bool, bool],
) -> None:
    await _seed_demo_target()
    asker = _tenant_admin(web=False)
    answer = await explain_call(
        CallQuestion(
            asker=asker, me=True, connector_id=_CONNECTOR_ID, op_id=_OP, target=target_name
        )
    )
    assert answer.answer == "wait", answer
    own = answer.own_approval
    assert own is not None
    assert own.model_dump() == {
        "web": None,
        "web_reason": DEPENDS,
        "token": None,
        "token_reason": DEPENDS,
    }

    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, target_name)
    result = await call(asker, _OP, params, connector_id=_CONNECTOR_ID, target=target)
    assert result.status == "awaiting_approval", result
    request_id = uuid.UUID(str(result.extras["approval_request_id"]))
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
        assert request is not None
        card = (request.proposed_effect or {})["preview"]["grant"]
    assert card["login_realm"] is login_realm
    done = (
        await _approve(request_id, web=True, params=params),
        await _approve(request_id, web=False, params=params),
    )
    assert done == approve
