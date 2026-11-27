# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``own_approval`` is ``null`` for a person where the approve reads the values of the call.

Explain asks without values. For an operation registered with
``register_rights_change_check``, ``register_grant_op`` or
``register_requester_check``, ``web`` and ``token`` are ``null`` for a
person and both reasons say why. An agent login and a service login read
``false`` with the text of the approve sign: the approve refuses them
before it reads any values. The real call with values, then the real
approve of the stored request, show the answer:

* case A: a change of rights only for some values;
* case B: a change of the rights of one login that the values name;
* case C: ``policy.apply``, with a check of who may ask.

All names and values are made up.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.connectors.policy.apply import register_policy_apply_operation
from meho_backplane.connectors.policy.classify import POLICY_REGISTRY_CONNECTOR_ID
from meho_backplane.main import app
from meho_backplane.operations.approval_queue import NonHumanApprovalError
from meho_backplane.operations.grant_approval import (
    RIGHTS_CHANGE_CONSOLE_ONLY,
    RIGHTS_CHANGE_REFUSAL,
    register_grant_op,
    register_rights_change_check,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._own_approval_cases import (
    ADMIN_TOKEN,
    ADMIN_WEB,
    AGENT,
    DEPENDS,
    MACHINE,
    OPERATOR_TOKEN,
    OPERATOR_WEB,
    PLATFORM_TOKEN,
    PLATFORM_WEB,
    SERVICE,
    Caller,
    approve,
    call,
    embedding_service,
    isolate_registries,
    register,
    switches,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_NULL = {"web": None, "web_reason": DEPENDS, "token": None, "token_reason": DEPENDS}
_SETTINGS = ["false", "true"]


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


async def _null_then_call(
    client: TestClient,
    key: Any,
    caller: Caller,
    op_id: str,
    params: dict[str, Any],
    *,
    connector_id: str = "demo-1.x",
) -> dict[str, tuple[bool, str]] | str:
    """Explain says ``null`` for a person and ``false`` for a machine login; then the call.

    Returns what the real approve of the call's request does for each way,
    or the refusal of the call when it does not wait. The approve refuses
    a machine login with :class:`NonHumanApprovalError`.
    """
    body = caller.ask(client, key, op_id, connector_id=connector_id)
    assert body["answer"] == "wait", (caller.name, body)
    part = MACHINE if caller.machine else _NULL
    assert body["own_approval"] == part, (caller.name, body["own_approval"])
    result = await call(caller.operator(), op_id, params, connector_id=connector_id)
    if result.status != "awaiting_approval":
        return str(result.error or result)
    request_id = uuid.UUID(str(result.extras["approval_request_id"]))
    done = {}
    for way, web in (("web", True), ("token", False)):
        outcome = await approve(caller, request_id, web=web, params=params)
        if caller.machine:
            assert type(outcome.error) is NonHumanApprovalError, (caller.name, way, outcome)
        done[way] = (outcome.accepted, outcome.reason)
    return done


def _yes_no(done: dict[str, tuple[bool, str]] | str) -> Any:
    if isinstance(done, str):
        return done
    return (done["web"][0], done["token"][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
async def test_case_a_a_change_of_rights_only_for_some_values(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    isolate_registries(monkeypatch)
    register_rights_change_check("demo.thing.write", lambda params: params.get("side") == "kc")
    await register(embedding, "demo.thing.write", "caution", params=("side",))
    key = make_rsa_keypair(f"kid-ownappr-case-a-{self_approval}")
    callers = (OPERATOR_TOKEN, ADMIN_TOKEN, PLATFORM_TOKEN, AGENT, SERVICE)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        found = {
            caller.name: await _null_then_call(
                client, key, caller, "demo.thing.write", {"side": "kc"}
            )
            for caller in callers
        }
    assert found[OPERATOR_TOKEN.name] == {
        "web": (False, RIGHTS_CHANGE_REFUSAL),
        "token": (False, RIGHTS_CHANGE_REFUSAL),
    }
    for admin in (ADMIN_TOKEN, PLATFORM_TOKEN):
        assert found[admin.name] == {
            "web": (True, ""),
            "token": (False, RIGHTS_CHANGE_CONSOLE_ONLY),
        }
    for machine in (AGENT, SERVICE):
        assert _yes_no(found[machine.name]) == (False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
async def test_case_b_a_change_of_the_rights_of_one_login(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
) -> None:
    switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=self_approval)
    isolate_registries(monkeypatch)
    register_grant_op("demo.login.grant", user_id_params=("user_id",))
    await register(embedding, "demo.login.grant", "caution", params=("user_id",))
    key = make_rsa_keypair(f"kid-ownappr-case-b-{self_approval}")
    callers = (
        PLATFORM_WEB,
        PLATFORM_TOKEN,
        ADMIN_WEB,
        ADMIN_TOKEN,
        OPERATOR_WEB,
        OPERATOR_TOKEN,
    )
    params = {"user_id": str(uuid.uuid4())}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        found = {
            caller.name: _yes_no(
                await _null_then_call(client, key, caller, "demo.login.grant", params)
            )
            for caller in callers
        }
        # With the switch on, only a person may ask for a change of rights.
        for machine in (AGENT, SERVICE):
            body = machine.ask(client, key, "demo.login.grant")
            assert body["answer"] == "deny", (machine.name, body)
            result = await call(machine.operator(), "demo.login.grant", params)
            assert result.status == "denied", (machine.name, result)
            assert "only a person may ask for a change" in str(result.error)
    expected = dict.fromkeys((caller.name for caller in callers), (False, False))
    expected[PLATFORM_WEB.name] = (True, False)
    assert found == expected


_POLICY_FILE = "policy: v1\nclassifications: []\n"


def _policy_params(scope: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "file": _POLICY_FILE,
        "file_sha256": hashlib.sha256(_POLICY_FILE.encode()).hexdigest(),
        "based_on": "0" * 64,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("self_approval", _SETTINGS)
async def test_case_c_an_operation_with_a_check_of_who_may_ask(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    self_approval: str,
) -> None:
    switches(
        monkeypatch,
        CUSTOM_POLICY="true",
        POLICY_CLASSIFICATION="true",
        APPROVAL_SIGN_IN_LEVEL="observe",
        APPROVAL_ALLOW_SELF_APPROVAL=self_approval,
    )
    await register_policy_apply_operation(embedding_service=embedding)
    key = make_rsa_keypair(f"kid-ownappr-case-c-{self_approval}")
    found: dict[tuple[str, str], Any] = {}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        for caller in (ADMIN_TOKEN, ADMIN_WEB, PLATFORM_TOKEN, PLATFORM_WEB):
            for scope in ("install", "tenant"):
                found[(caller.name, scope)] = _yes_no(
                    await _null_then_call(
                        client,
                        key,
                        caller,
                        "policy.apply",
                        _policy_params(scope),
                        connector_id=POLICY_REGISTRY_CONNECTOR_ID,
                    )
                )
        for machine in (AGENT, SERVICE):
            body = machine.ask(
                client, key, "policy.apply", connector_id=POLICY_REGISTRY_CONNECTOR_ID
            )
            assert body["answer"] == "deny", (machine.name, body)
            assert "own_approval" not in body, (machine.name, body)
            for scope in ("install", "tenant"):
                result = await call(
                    machine.operator(),
                    "policy.apply",
                    _policy_params(scope),
                    connector_id=POLICY_REGISTRY_CONNECTOR_ID,
                )
                assert result.status != "awaiting_approval", (machine.name, scope, result)
    for admin in (ADMIN_TOKEN, ADMIN_WEB):
        assert "Only a platform admin can ask to import the install's policy." in str(
            found[(admin.name, "install")]
        )
        assert found[(admin.name, "tenant")] == (True, False)
    for platform in (PLATFORM_TOKEN, PLATFORM_WEB):
        assert (
            found[(platform.name, "install")]
            == found[(platform.name, "tenant")]
            == (
                True,
                False,
            )
        )


@pytest.mark.asyncio
async def test_a_question_with_groups_for_a_machine_login_reads_false(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The made-up login of a question with groups: a machine login reads ``false``."""
    switches(monkeypatch, CUSTOM_POLICY="true")
    isolate_registries(monkeypatch)
    register_rights_change_check("demo.thing.write", lambda params: params.get("side") == "kc")
    await register(embedding, "demo.thing.write", "caution", params=("side",))
    key = make_rsa_keypair("kid-ownappr-groups-machine")
    found = {}
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        for principal in ("agent", "service", "person"):
            body = PLATFORM_TOKEN.ask(
                client, key, "demo.thing.write", groups=["Operators"], principal=principal
            )
            assert body["answer"] == "wait", (principal, body)
            found[principal] = body["own_approval"]
    assert found == {"agent": MACHINE, "service": MACHINE, "person": _NULL}
