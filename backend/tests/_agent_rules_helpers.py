# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared set-up for the tests of the personal agent rules in a tenant's policy file.

Used by ``test_api_v1_policy_agent_rules.py`` and
``test_api_v1_policy_agent_rules_card.py``. Builds on
``_install_limits_helpers.py``: the same fixture operations
(``inslim.vm.list`` is ``safe``, ``inslim.vm.restart`` is ``caution``), the
same token, REST and web console helpers.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.service import build_plan
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session

from ._install_limits_helpers import CONNECTOR, TENANT, store_install, token

RULE = (
    "  - name: helper\n"
    "    caller: personal_agent\n"
    "    roles: [Admin, Operator]\n"
    "    up_to: dangerous\n"
)
FILE_A = (
    "policy: v1\nclassifications:\n  - {match: {connector: inslim-1.x}, category: network}\n"
    f"agents:\n{RULE}"
)
#: :data:`FILE_A` with a stored binding: the export of a tenant without stored
#: bindings holds the shipped ones, so only a file with bindings exports as it is.
FILE_BOUND = (
    FILE_A + 'bindings:\n  - {match: {group: /MEHO-Admins}, kind: Admins, categories: ["*"]}\n'
)
#: The tenant file without the rules, with another description.
FILE_NO_RULES = (
    "policy: v1\ndescription: an unrelated edit\nclassifications:\n"
    "  - {match: {connector: inslim-1.x}, category: network}\n"
)


async def set_limit(level: str) -> None:
    """Store the install's policy with the outer limit *level*."""
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            TenantPolicy.__table__.delete().where(TenantPolicy.tenant_id.is_(None))
        )
    await store_install({"classifications": [], "limits": {"personal_agent_up_to": level}})


async def stored_tenant() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id == TENANT))
        ).scalar_one_or_none()
    return None if row is None else row.document


def tenant_admin(key: Any, sub: str = "tenant-admin-a", **claims: Any) -> str:
    """A tenant admin's token, not a platform admin."""
    return token(key, sub=sub, platform_admin=False, **claims)


def export_tenant(client: TestClient, bearer: str) -> Any:
    return client.get("/api/v1/policy/export", headers={"Authorization": f"Bearer {bearer}"})


def inslim(items: list[dict[str, str]]) -> list[str]:
    """The fixture operations in one role's list of the plan."""
    return [item["op_id"] for item in items if item["connector_id"] == "inslim-1.x"]


async def dispatch_tenant_apply(operator: Operator, content: str) -> Any:
    """Call ``policy.apply`` for the tenant's file, planned with the switch as it is now."""
    async with get_sessionmaker()() as session:
        plan = await build_plan(
            session,
            scope="tenant",
            tenant_id=TENANT,
            content=content.encode(),
            custom_policy=custom_policy_on(get_settings()),
        )
    return await dispatch(
        operator=operator,
        connector_id=CONNECTOR,
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params={
            "scope": "tenant",
            "file": content,
            "file_sha256": file_sha256(content.encode()),
            "based_on": plan.based_on,
        },
    )


async def console_get(client: TestClient, key: Any, path: str, *, sub: str) -> Any:
    """Open *path* in the web console as the tenant admin *sub*, after a console sign-in."""
    signed_in = tenant_admin(
        key,
        sub=sub,
        extra_claims={
            "auth_time": int(time.time()),
            "acr": "1",
            "sid": str(uuid.uuid4()),
            "azp": "meho-web",
        },
    )
    async with get_sessionmaker()() as session, session.begin():
        web = await create_session(
            session,
            operator_sub=sub,
            tenant_id=TENANT,
            access_token=signed_in,
            refresh_token="refresh-token",
            lifetime=timedelta(hours=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, str(web.id))
    try:
        return client.get(path, follow_redirects=False)
    finally:
        client.cookies.clear()
