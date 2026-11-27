# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``limits`` part and the reserved keys through ``/api/v1/policy`` and ``policy.apply``.

* With the switch off, a stored ``limits`` part is ignored: the export has
  no ``limits``, the preview of that export is "No change.", and an
  approved import keeps the stored part.
* ``apply_policy`` plans with the switch value it is given: the run after
  the approval refuses a file with ``limits`` while the switch is off.
* The five refused keys, in any form, are refused with one sentence.
* A file that holds the key ``limits`` is refused whatever its value, where
  ``limits`` is not allowed; nothing waits.
* The refusal is the same sentence whether the environment sets
  ``CUSTOM_POLICY`` or not.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import (
    POLICY_APPLY_OP_ID,
    register_policy_apply_operation,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.agent_rules import AGENTS_IN_INSTALL_FILE, AGENTS_NEED_CUSTOM_POLICY
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.limits import LIMITS_NEED_CUSTOM_POLICY, personal_agent_limit
from meho_backplane.policy.service import apply_policy, build_plan
from meho_backplane.policy.validate import check_document
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)

_FILE_P = "policy: v1\nclassifications:\n  - {match: {connector: limapi-1.x}, category: network}\n"
_FILE_L = _FILE_P + "limits: {personal_agent_up_to: dangerous}\n"
_FILE_P2 = _FILE_P + "  - {match: {op: limapi.*}, always_wait: true}\n"

_TENANT_SENTENCE = (
    "`limits` belongs to the install's policy file. Only a platform admin sets the "
    "outer limit; a tenant's file stays inside it."
)


def _not_read(key: str) -> str:
    return f"This version of MEHO does not read `{key}` in a policy file; remove it."


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _setup(embedding: AsyncMock) -> None:
    await register_policy_apply_operation(embedding_service=embedding)
    await register_typed_operation(
        product="limapi",
        version="1.x",
        impl_id="limapi",
        op_id="limapi.vm.list",
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="compute",
        when_to_use="Fixture group.",
        embedding_service=embedding,
    )


def _token(key: Any, *, platform_admin: bool = False) -> str:
    return mint_token(
        key,
        sub="admin-a",
        tenant_id=str(_TENANT),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
    )


def _post(client: TestClient, path: str, token: str, content: str, **params: str) -> Any:
    return client.post(
        f"/api/v1/policy/{path}",
        params=params,
        content=content.encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
    )


def _admin(sub: str, *, web: bool) -> Operator:
    return Operator(
        sub=sub,
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        platform_admin=True,
        raw_jwt="header.payload.signature",
        web_session=web,
        acr="1",
        auth_time=int(time.time()),
    )


async def _stored_install() -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(TenantPolicy).where(TenantPolicy.tenant_id.is_(None)))
        ).scalar_one_or_none()
    return None if row is None else row.document


async def _requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


async def _approve(request_id: uuid.UUID) -> Any:
    approver = _admin("admin-b", web=True)
    async with get_sessionmaker()() as session:
        request = await approve_request(session, request_id, operator=approver)
        await session.commit()
    return await resume_dispatch_after_approval(operator=approver, request=request)


async def _store_with_the_switch_on(content: str) -> None:
    async with get_sessionmaker()() as session, session.begin():
        plan = await build_plan(
            session, scope="install", tenant_id=None, content=content.encode(), custom_policy=True
        )
        await apply_policy(
            session,
            scope="install",
            tenant_id=None,
            content=content.encode(),
            expected_sha256=file_sha256(content.encode()),
            based_on=plan.based_on,
            imported_by="fixture",
            approved_by="fixture",
            custom_policy=True,
        )


# ---------------------------------------------------------------------------
# A stored ``limits`` part with the switch off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_with_the_switch_off_a_stored_limit_is_ignored_on_every_path(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _setup(embedding)
    await _store_with_the_switch_on(_FILE_L)
    key = make_rsa_keypair("kid-lim-stored")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, platform_admin=True)
        exported = client.get(
            "/api/v1/policy/export",
            params={"scope": "install"},
            headers={"Authorization": f"Bearer {token}"},
        )
        again = _post(client, "import", token, exported.text, scope="install", preview="true")
        same = _post(client, "import", token, _FILE_P, scope="install", preview="true")
        changed = _post(client, "import", token, _FILE_P2, scope="install")
    assert exported.status_code == 200
    assert "limits" not in exported.text
    assert "limapi-1.x" in exported.text
    for preview in (again.json(), same.json()):
        assert preview["ok"] is True, preview
        assert preview["no_change"] is True
        assert "limits" not in preview["diff"]
    assert changed.status_code == 202, changed.text

    result = await _approve(uuid.UUID(changed.json()["approval_request_id"]))
    assert result.status == "ok", result
    assert not any(k.startswith("limits_") for k in result.result["summary"])
    stored = await _stored_install()
    assert stored is not None
    assert stored["limits"] == {"personal_agent_up_to": "dangerous"}
    assert {"match": {"op": "limapi.*"}, "always_wait": True} in stored["classifications"]
    assert personal_agent_limit(stored) == "safe"
    assert personal_agent_limit(stored, custom_policy=True) == "dangerous"


# ---------------------------------------------------------------------------
# apply_policy plans with the switch value it is given
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
async def test_the_approved_run_refuses_limits_with_the_switch_off(embedding: AsyncMock) -> None:
    await _setup(embedding)
    async with get_sessionmaker()() as session:
        plan = await build_plan(session, scope="install", tenant_id=None, content=_FILE_P.encode())
    result = await dispatch(
        operator=_admin("admin-a", web=False),
        connector_id="policy-registry-1.x",
        op_id=POLICY_APPLY_OP_ID,
        target=None,
        params={
            "scope": "install",
            "file": _FILE_L,
            "file_sha256": file_sha256(_FILE_L.encode()),
            "based_on": plan.based_on,
        },
    )
    assert result.status == "awaiting_approval", result
    (request,) = await _requests()

    run = await _approve(request.id)
    assert run.status != "ok", run
    assert LIMITS_NEED_CUSTOM_POLICY in str(run.error) + str(run.extras)
    assert await _stored_install() is None


# ---------------------------------------------------------------------------
# The five refused keys
# ---------------------------------------------------------------------------

_FIVE = ["approvers", "role_bindings", "risk_levels", "custom_roles", "baseline"]
_FORMS = ["{example-name: example-value-7}", "[example-value-7]", "example-value-7", "null"]


@pytest.mark.parametrize("scope", ["tenant", "install"])
def test_each_refused_key_in_each_form_is_one_sentence(client: TestClient, scope: str) -> None:
    key = make_rsa_keypair(f"kid-lim-six-{scope}")
    answers = 0
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, platform_admin=scope == "install")
        for name in _FIVE:
            for form in _FORMS:
                content = f"policy: v1\n{name}: {form}\n"
                for path, extra in (("validate", {}), ("import", {"preview": "true"})):
                    resp = _post(client, path, token, content, scope=scope, **extra)
                    assert resp.status_code == 200, resp.text
                    assert resp.json()["ok"] is False
                    assert resp.json()["mistakes"] == [_not_read(name)]
                    assert "example-value-7" not in resp.text
                    answers += 1
    assert answers == 5 * 4 * 2


# ---------------------------------------------------------------------------
# The key ``limits`` is refused whatever its value
# ---------------------------------------------------------------------------

_EMPTY_FORMS = ["limits: {}", "limits: null", "limits:", "limits: {personal_agent_up_to: null}"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
@pytest.mark.parametrize(
    ("scope", "sentence"),
    [("tenant", _TENANT_SENTENCE), ("install", LIMITS_NEED_CUSTOM_POLICY)],
)
async def test_an_empty_limits_part_is_refused_and_nothing_waits(
    client: TestClient, embedding: AsyncMock, scope: str, sentence: str
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair(f"kid-lim-empty-{scope}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, platform_admin=scope == "install")
        for form in _EMPTY_FORMS:
            content = f"policy: v1\nclassifications: []\n{form}\n"
            for path, extra in (("validate", {}), ("import", {"preview": "true"})):
                resp = _post(client, path, token, content, scope=scope, **extra)
                assert resp.status_code == 200, resp.text
                assert resp.json()["ok"] is False
                assert resp.json()["mistakes"] == [sentence], form
            resp = _post(client, "import", token, content, scope=scope)
            assert resp.status_code == 422, resp.text
            assert resp.json()["ok"] is False
            assert resp.json()["mistakes"] == [sentence], form
    assert await _requests() == []
    assert await _stored_install() is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch")
@pytest.mark.parametrize("scope", ["tenant", "install"])
@pytest.mark.parametrize(
    ("form", "sentence"),
    [
        ("limits: []", "`limits` must be a mapping of named keys."),
        ("limits: true", "`limits` must be a mapping of named keys."),
        ("limits: {personal_agent_up_to: 7}", "`limits.personal_agent_up_to` must be text."),
    ],
)
async def test_a_limits_part_of_the_wrong_type_is_refused(
    client: TestClient, embedding: AsyncMock, scope: str, form: str, sentence: str
) -> None:
    await _setup(embedding)
    key = make_rsa_keypair(f"kid-lim-type-{scope}")
    content = f"policy: v1\n{form}\n"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        token = _token(key, platform_admin=scope == "install")
        for path, extra in (("validate", {}), ("import", {"preview": "true"})):
            resp = _post(client, path, token, content, scope=scope, **extra)
            assert resp.json()["ok"] is False
            assert resp.json()["mistakes"] == [sentence]
        resp = _post(client, "import", token, content, scope=scope)
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [sentence]
    assert await _requests() == []


# ---------------------------------------------------------------------------
# The five refused keys are one sentence with the switch on and off; ``limits`` follows it
# ---------------------------------------------------------------------------

_SEVEN_FILES = {
    "limits": "limits: {personal_agent_up_to: caution}",
    "agents": "agents: {example-name: example-value-7}",
    **{name: f"{name}: {{example-name: example-value-7}}" for name in _FIVE},
}


@pytest.mark.parametrize("environment", [None, "true"])
def test_the_five_keys_are_refused_whatever_the_environment_and_limits_follows_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, environment: str | None
) -> None:
    if environment is None:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    else:
        monkeypatch.setenv("CUSTOM_POLICY", environment)
    get_settings.cache_clear()
    key = make_rsa_keypair(f"kid-lim-seven-{environment}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        install = _token(key, platform_admin=True)
        for name, line in _SEVEN_FILES.items():
            content = f"policy: v1\n{line}\n"
            for path, extra in (("validate", {}), ("import", {"preview": "true"})):
                resp = _post(client, path, install, content, scope="install", **extra)
                if name == "limits" and environment == "true":
                    assert resp.json()["ok"] is True, resp.text
                    assert resp.json()["mistakes"] == []
                elif name == "limits":
                    assert resp.json()["mistakes"] == [LIMITS_NEED_CUSTOM_POLICY]
                elif name == "agents":
                    sentence = AGENTS_IN_INSTALL_FILE if environment else AGENTS_NEED_CUSTOM_POLICY
                    assert resp.json()["mistakes"] == [sentence]
                else:
                    assert resp.json()["mistakes"] == [_not_read(name)]
        tenant = _post(client, "validate", _token(key), f"policy: v1\n{_SEVEN_FILES['limits']}\n")
    assert tenant.json()["mistakes"] == [_TENANT_SENTENCE]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_only_the_caller_opens_limits_in_the_install_file() -> None:
    async with get_sessionmaker()() as session:
        for name in _FIVE:
            plan = await build_plan(
                session,
                scope="install",
                tenant_id=None,
                content=f"policy: v1\n{_SEVEN_FILES[name]}\n".encode(),
                custom_policy=True,
            )
            assert plan.mistakes == [_not_read(name)]
        content = f"policy: v1\n{_SEVEN_FILES['limits']}\n".encode()
        plan = await build_plan(
            session, scope="install", tenant_id=None, content=content, custom_policy=True
        )
    assert plan.mistakes == []
    document = plan.document
    assert document is not None
    assert check_document(document, scope="install", install_document=document, ops=[]) == [
        LIMITS_NEED_CUSTOM_POLICY
    ]
    assert (
        check_document(
            document, scope="install", install_document=document, ops=[], custom_policy=True
        )
        == []
    )
