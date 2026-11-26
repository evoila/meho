# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The target form route says whether the caller can run the test read now.

Each case calls ``GET /api/v1/connectors/target-form`` with a real token, then
makes the REAL call of the same read through ``POST /api/v1/operations/call``
for the same caller, and checks that the route's answer agrees with the call's
result. The route holds no copy of the rule: it uses the call's own row lookup
and policy (``operations/run_now_check.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.connectors_target_form import router as form_router
from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.principal_block import BLOCK_SCOPE_ALL
from meho_backplane.connectors import Connector
from meho_backplane.connectors import registry as connector_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationPolicyOverride,
    PrincipalBlock,
    TenantOperationSetting,
    TenantPolicy,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_KEY = make_rsa_keypair("kid-run-now")
_PRODUCT, _VERSION, _IMPL = "runfake", "1.x", "runfake-rest"
_CONNECTOR_ID = f"{_IMPL}-{_VERSION}"
_OP = "runfake.ping"
_FORM = "/api/v1/connectors/target-form"
_CALL = "/api/v1/operations/call"
_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a701")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a702")
#: Made-up values that must never reach an answer.
_MADE_UP = "made-up-reason-7f3a91c2"


async def _handler(operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True}


class _RunFake(Connector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL
    test_operation = _OP

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, _VERSION, _IMPL), _RunFake)
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(form_router)
    app.include_router(operations_router)
    yield TestClient(app)


def _row(**values: Any) -> EndpointDescriptor:
    fields: dict[str, Any] = {
        "tenant_id": None,
        "product": _PRODUCT,
        "version": _VERSION,
        "impl_id": _IMPL,
        "op_id": _OP,
        "source_kind": "typed",
        "handler_ref": f"{__name__}._handler",
        "summary": "Ping.",
        "description": "Ping.",
        "tags": [],
        "parameter_schema": {"type": "object"},
        "safety_level": "safe",
        "requires_approval": False,
        "is_enabled": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fields.update(values)
    return EndpointDescriptor(**fields)


async def _add(*objects: Any) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add_all(objects)


def _token(**kwargs: Any) -> str:
    kwargs.setdefault("sub", "op-run-now")
    kwargs.setdefault("tenant_id", str(_TENANT))
    kwargs.setdefault("tenant_role", "operator")
    return mint_token(_KEY, **kwargs)


def _ask(client: TestClient, token: str) -> tuple[Any, Any]:
    """The route's answer and the real call's answer, for the same token."""
    headers = {"Authorization": f"Bearer {token}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
        form = client.get(_FORM, params={"product": _PRODUCT}, headers=headers)
        call = client.post(
            _CALL,
            json={"connector_id": _CONNECTOR_ID, "op_id": _OP, "target": None, "params": {}},
            headers=headers,
        )
    return form, call


def _run_answer(form: Any) -> dict[str, Any]:
    assert form.status_code == 200, form.text
    (answer,) = form.json()["classes"]
    assert answer["test_operation"] == _OP
    run: dict[str, Any] = answer["test_operation_run"]
    return run


def _assert_runs(client: TestClient, token: str) -> None:
    form, call = _ask(client, token)
    assert _run_answer(form) == {"can_run": True, "code": None, "reason": None}
    assert call.status_code == 200, call.text
    assert call.json()["status"] == "ok", call.text


def _assert_refused(client: TestClient, token: str, code: str) -> dict[str, Any]:
    form, call = _ask(client, token)
    run = _run_answer(form)
    assert (run["can_run"], run["code"]) == (False, code)
    assert isinstance(run["reason"], str)
    assert call.status_code == 200, call.text
    body = call.json()
    assert body["status"] == "error", body
    assert body["extras"]["error_code"] == "unknown_op", body
    return run


def _assert_waits(client: TestClient, token: str) -> dict[str, Any]:
    form, call = _ask(client, token)
    run = _run_answer(form)
    assert (run["can_run"], run["code"]) == (False, "waits_for_approval")
    assert call.status_code == 200, call.text
    assert call.json()["status"] == "awaiting_approval", call.text
    return run


@pytest.mark.asyncio
async def test_a_safe_enabled_read_with_no_override_can_run(client: TestClient) -> None:
    await _add(_row())
    _assert_runs(client, _token())


@pytest.mark.asyncio
async def test_a_read_turned_off_for_the_tenant_is_not_enabled(client: TestClient) -> None:
    await _add(
        _row(),
        TenantOperationSetting(
            tenant_id=_TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            op_id=_OP,
            enabled=False,
            set_by_sub="admin",
            set_at=datetime.now(UTC),
        ),
    )
    run = _assert_refused(client, _token(), "not_enabled")
    assert run["reason"].startswith("This test read is not turned on")


@pytest.mark.asyncio
async def test_a_tenant_with_its_own_rows_without_the_read_is_not_enabled(
    client: TestClient,
) -> None:
    await _add(_row(), _row(tenant_id=_TENANT, op_id="runfake.other", source_kind="typed"))
    _assert_refused(client, _token(), "not_enabled")
    # Another tenant has no rows of its own and still runs the built-in read.
    _assert_runs(client, _token(tenant_id=str(_OTHER_TENANT)))


@pytest.mark.asyncio
async def test_a_tenant_can_turn_the_read_on_while_the_install_default_is_off(
    client: TestClient,
) -> None:
    await _add(
        _row(is_enabled=False),
        TenantOperationSetting(
            tenant_id=_TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            op_id=_OP,
            enabled=True,
            set_by_sub="admin",
            set_at=datetime.now(UTC),
        ),
    )
    _assert_runs(client, _token())
    # Another tenant has no setting, so the install default (off) holds for it.
    _assert_refused(client, _token(tenant_id=str(_OTHER_TENANT)), "not_enabled")


@pytest.mark.asyncio
async def test_a_stored_approval_flag_waits_for_approval(client: TestClient) -> None:
    await _add(_row(requires_approval=True))
    run = _assert_waits(client, _token())
    assert run["reason"] == "A call of this test read waits until a person approves it."


@pytest.mark.asyncio
async def test_a_stored_risk_level_above_safe_waits_for_approval(client: TestClient) -> None:
    await _add(_row(safety_level="dangerous", requires_approval=True))
    _assert_waits(client, _token())


@pytest.mark.asyncio
async def test_a_classification_rule_that_raises_the_level_waits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    await _add(
        _row(),
        TenantPolicy(
            tenant_id=None,
            document={"classifications": [{"match": {"op": _OP}, "risk": "caution"}]},
            set_by_sub="admin",
        ),
    )
    _assert_waits(client, _token())


@pytest.mark.asyncio
async def test_the_classification_is_not_read_while_its_switch_is_off(
    client: TestClient,
) -> None:
    await _add(
        _row(),
        TenantPolicy(
            tenant_id=None,
            document={"classifications": [{"match": {"op": _OP}, "risk": "caution"}]},
            set_by_sub="admin",
        ),
    )
    _assert_runs(client, _token())


@pytest.mark.asyncio
async def test_an_override_of_another_tenant_does_not_change_the_answer(
    client: TestClient,
) -> None:
    await _add(
        _row(),
        OperationPolicyOverride(
            tenant_id=_OTHER_TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            op_id=_OP,
            safety_level="dangerous",
            requires_approval=True,
            reason=_MADE_UP,
            set_by_sub="admin",
        ),
    )
    _assert_runs(client, _token())
    _assert_waits(client, _token(tenant_id=str(_OTHER_TENANT)))


@pytest.mark.asyncio
async def test_an_override_of_the_callers_tenant_waits_and_shows_no_stored_value(
    client: TestClient,
) -> None:
    await _add(
        _row(summary=_MADE_UP, description=_MADE_UP),
        OperationPolicyOverride(
            tenant_id=_TENANT,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_IMPL,
            op_id=_OP,
            safety_level="caution",
            requires_approval=True,
            reason=_MADE_UP,
            set_by_sub=_MADE_UP,
        ),
    )
    form, call = _ask(client, _token())
    run = _run_answer(form)
    assert run == {
        "can_run": False,
        "code": "waits_for_approval",
        "reason": "A call of this test read waits until a person approves it.",
    }
    assert call.json()["status"] == "awaiting_approval"
    assert _MADE_UP not in form.text
    assert str(_TENANT) not in form.text
    assert "caution" not in form.text


@pytest.mark.asyncio
async def test_an_agent_with_no_grant_is_not_allowed_while_grants_are_required(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "on")
    get_settings.cache_clear()
    await _add(_row())
    form, call = _ask(client, _token(sub="agent-run-now", principal_kind="agent"))
    run = _run_answer(form)
    assert (run["can_run"], run["code"]) == (False, "not_allowed")
    assert run["reason"] == "Your login is not allowed to run this test read."
    assert call.status_code == 200, call.text
    assert call.json()["status"] == "denied", call.text
    # A person is not an agent: the same read runs for them.
    _assert_runs(client, _token())


@pytest.mark.asyncio
async def test_a_reader_gets_403_and_the_call_is_refused(client: TestClient) -> None:
    await _add(_row())
    form, call = _ask(client, _token(sub="reader", tenant_role="read_only"))
    assert form.status_code == 403
    assert call.status_code == 403


@pytest.mark.asyncio
async def test_a_blocked_login_gets_403_and_the_call_is_refused(client: TestClient) -> None:
    await _add(
        _row(),
        PrincipalBlock(
            sub="blocked-run-now",
            scope=BLOCK_SCOPE_ALL,
            reason=_MADE_UP,
            blocked_by_sub="admin",
            blocked_at=datetime.now(UTC),
        ),
    )
    form, call = _ask(client, _token(sub="blocked-run-now"))
    assert form.status_code == 403
    assert call.status_code == 403
    assert _MADE_UP not in form.text


@pytest.mark.asyncio
async def test_a_class_with_no_test_read_has_no_run_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    none_declared = type("NoTestRead", (_RunFake,), {"test_operation": None})
    monkeypatch.setitem(connector_registry._REGISTRY_V2, (_PRODUCT, _VERSION, _IMPL), none_declared)
    form, _call = _ask(client, _token())
    (answer,) = form.json()["classes"]
    assert answer["test_operation"] is None
    assert answer["test_operation_run"] is None
