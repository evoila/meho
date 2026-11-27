# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``own_approval`` of ``meho policy explain`` with approver routing.

* The whole explain answer for an Operator, a tenant admin and a platform
  admin who asks with groups, compared with the expected answer
  (``snapshots/policy_explain_own_approval_routing.json``). A caller who
  is not a tenant admin or a platform admin reads the reasons without the
  places of routing entries.
* A change of rights for every call with a routing entry: it is not routed.
* A tenant routing entry that refuses: the approve gives the reason of the
  stored groups, not of the install's routing.
* A question with groups from inside a hosted agent run asks as a login of
  its own, not as the run.

All names and values are made up.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations.grant_approval import register_rights_change_op
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.policy.explain_asker import what_if_operator
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._own_approval_cases import (
    ADMIN_TOKEN,
    OPERATOR_TOKEN,
    PLATFORM_TOKEN,
    TENANT,
    Caller,
    agrees,
    embedding_service,
    isolate_registries,
    op,
    register,
    row_of,
    switches,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_EXPECTED = Path(__file__).parent / "snapshots" / "policy_explain_own_approval_routing.json"


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    return embedding_service()


async def _routing(scope: str, approvers: list[str]) -> None:
    document = {
        "policy": "v1",
        "routing": [{"match": {"connector": "demo-*"}, "approvers": approvers}],
    }
    async with get_sessionmaker()() as session:
        session.add(
            TenantPolicy(
                tenant_id=None if scope == "install" else TENANT,
                document=document,
                set_by_sub="fixture",
            )
        )
        await session.commit()


async def _case_e_answers(client: TestClient, key: Any) -> dict[str, Any]:
    caution = op("caution")
    answers = {
        "operator": OPERATOR_TOKEN.ask(client, key, caution),
        "tenant_admin": ADMIN_TOKEN.ask(client, key, caution),
        "platform_admin_with_groups": PLATFORM_TOKEN.ask(
            client, key, caution, groups=["Operators"]
        ),
    }
    for caller in (OPERATOR_TOKEN, ADMIN_TOKEN):
        row = await row_of(client, key, caller, caution)
        assert agrees(row) == [] or caller is OPERATOR_TOKEN, (caller.name, row)
        answers[f"{caller.sub} approve"] = {
            way: [done.accepted, done.reason] for way, done in row.done.items()
        }
    return answers


@pytest.mark.asyncio
async def test_case_e_the_whole_answer_names_a_place_only_for_an_admin(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="true")
    await register(embedding, op("caution"), "caution")
    await _routing("install", ["Admins"])
    key = make_rsa_keypair("kid-ownappr-case-e")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        answers = await _case_e_answers(client, key)
    assert answers == json.loads(_EXPECTED.read_text())
    operator = answers["operator"]["own_approval"]["token_reason"]
    assert "MEHO-Admins" in operator and "MEHO-Operators" in operator
    assert "routing[" not in json.dumps(answers["operator"])
    platform = answers["platform_admin_with_groups"]["own_approval"]["token_reason"]
    assert "MEHO-Admins" in platform and "install:routing[0]" in platform
    # The approve itself names the place; the Operator reads the same sentence without it.
    approve_text = answers["op-token approve"]["token"][1]
    assert approve_text.replace(" (`install:routing[0]`)", "") == operator


#: A platform admin whose role in the tenant is ``operator``: an admin for a
#: change of rights, but in MEHO-Operators for the routing.
_PLATFORM_OPERATOR = Caller(
    "Platform admin, operator role, token",
    "pa-op-token",
    PrincipalKind.USER,
    TenantRole.OPERATOR,
    platform_admin=True,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [ADMIN_TOKEN, _PLATFORM_OPERATOR], ids=["admin", "platform"])
async def test_case_f_a_change_of_rights_for_every_call_is_not_routed(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, caller: Caller
) -> None:
    """Only an admin in the web console approves it; the routing entry plays no part."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="false")
    isolate_registries(monkeypatch)
    register_rights_change_op("demo.rights.write")
    await register(embedding, "demo.rights.write", "caution")
    await _routing("install", ["Operators"])
    key = make_rsa_keypair("kid-ownappr-case-f")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        row = await row_of(client, key, caller, "demo.rights.write")
    assert agrees(row) == []
    assert row.own is not None and (row.own["web"], row.own["token"]) == (True, False)
    assert (row.done["web"].accepted, row.done["token"].accepted) == (True, False)


_TENANT_REFUSAL = "Only MEHO-Admins may approve this change{}. You are in MEHO-Operators."


@pytest.mark.asyncio
async def test_a_tenant_routing_entry_that_refuses(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install's routing has no entry; the refusal comes from the stored groups."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="true")
    await register(embedding, op("caution"), "caution")
    await _routing("tenant", ["Admins"])
    key = make_rsa_keypair("kid-ownappr-tenant-routing")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        row = await row_of(client, key, OPERATOR_TOKEN, op("caution"))
    assert row.own == {
        "web": False,
        "web_reason": _TENANT_REFUSAL.format(""),
        "token": False,
        "token_reason": _TENANT_REFUSAL.format(""),
    }
    for way in ("web", "token"):
        done = row.done[way]
        assert not done.accepted
        assert done.reason == _TENANT_REFUSAL.format(" (routing rule `tenant:routing[0]`)")


@pytest.mark.asyncio
async def test_a_question_with_groups_in_a_hosted_run_asks_as_its_own_login(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's agent is not the asker: a caution call by token waits only for its token."""
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="false")
    await register(embedding, op("caution"), "caution")
    asker = what_if_operator(
        PLATFORM_TOKEN.operator(), groups=["Operators"], principal="person", via="token"
    )
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: "agent:hosted"}):
        answer = await explain_call(
            CallQuestion(
                asker=asker,
                me=False,
                connector_id="demo-1.x",
                op_id=op("caution"),
                groups=("Operators",),
            )
        )
    own = answer.own_approval
    assert answer.answer == "wait" and own is not None
    assert (own.web, own.token) == (True, False), own
