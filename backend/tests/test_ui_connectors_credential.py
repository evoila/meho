# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of the credential card on the target page.

The card (``GET /ui/connectors/{name}/credential``) shows the path the
credential is stored at, whether one is stored, and a form built from the
``credential_ways`` the connector declares. The submit (``POST`` of the same
path) runs ``vault.kv.put`` with only the fields of the chosen way.

The harness is the one of the Vault write tests: a small app with the console
session and CSRF layers, a session row with a signed token, and the Vault
connector with its operations registered. Most tests replace
``call_operation`` to read the arguments the route sends and to give the
answer under test. Two tests run the real dispatch: the wait for approval,
and the check that no value reaches the audit rows or the log.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult
from meho_backplane.connectors.target_form import CredentialField, CredentialWay
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.settings import get_settings
from tests._vault_fakes import install_fake_client
from tests.test_ui_vault_writes import (  # noqa: F401  (_bff_env is an autouse fixture)
    _OP_OPERATOR,
    _OP_READ_ONLY,
    _SECRET_VALUE,
    _TENANT_A,
    _awaiting_envelope,
    _bff_env,
    _client_with_role_and_csrf,
    _csrf_headers,
    _make_stub,
    _register_vault,
    _seed_tenant,
    _seed_vault_descriptor,
)

_TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OP_ADMIN = "op-admin"
_PASSWORD_FORM = {
    "way": "password",
    "field__password__username": "svc",
    "field__password__password": "x",
}

_WAYS = (
    CredentialWay(
        key="key",
        label="Private key",
        fields=(
            CredentialField(name="username", label="User name", hidden=False),
            CredentialField(
                name="private_key",
                label="Key text",
                help="The text of the private key.",
                multiline=True,
            ),
        ),
    ),
    CredentialWay(
        key="password",
        label="Password",
        fields=(
            CredentialField(name="username", label="User name", hidden=False),
            CredentialField(name="password", label="Password"),
        ),
    ),
)


class _Base(Connector):
    version = "1.0"
    supported_version_range = None

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:
        raise NotImplementedError  # pragma: no cover

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError  # pragma: no cover

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError  # pragma: no cover


class _WaysConnector(_Base):
    product = "credprod"
    impl_id = "credprod"
    credential_ways = _WAYS


class _CliConnector(_Base):
    product = "cliprod"
    impl_id = "cliprod"


class _NoCredentialConnector(_Base):
    product = "freeprod"
    impl_id = "freeprod"
    credential_ways = ()


def _register_connectors() -> None:
    for cls in (_WaysConnector, _CliConnector, _NoCredentialConnector):
        register_connector_v2(product=cls.product, version="1.0", impl_id=cls.impl_id, cls=cls)


def _seed_target(
    tenant_id: uuid.UUID,
    name: str,
    *,
    product: str = "credprod",
    secret_ref: str | None = "",
) -> None:
    ref = f"tenants/{tenant_id}/{name}" if secret_ref == "" else secret_ref

    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                TargetORM(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product=product,
                    host="host.example.test",
                    secret_ref=ref,
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={},
                )
            )

    asyncio.run(_do())


def _setup(stub: AsyncMock | None = None) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_vault_descriptor()
    _register_vault(stub or _make_stub())
    _register_connectors()


def _versions_ok(version: int = 3) -> dict[str, Any]:
    return {
        "status": "ok",
        "op_id": "vault.kv.versions",
        "result": {
            "current_version": version,
            "versions": {
                str(version): {
                    "created_time": "2026-11-20T10:00:00Z",
                    "deletion_time": "",
                    "destroyed": False,
                }
            },
        },
        "error": None,
        "extras": {},
    }


def _fault(exception_class: str) -> dict[str, Any]:
    return {
        "status": "error",
        "op_id": "x",
        "result": None,
        "error": "failed",
        "extras": {"error_code": "connector_error", "exception_class": exception_class},
    }


def _patch_ops(
    monkeypatch: pytest.MonkeyPatch,
    *,
    versions: dict[str, Any] | None = None,
    put: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Replace ``call_operation`` in both route modules; return every call's arguments."""
    calls: list[dict[str, Any]] = []

    async def _fake(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments)
        if arguments["op_id"] == "vault.kv.put":
            return put or _awaiting_envelope("vault.kv.put")
        return versions or _versions_ok()

    monkeypatch.setattr("meho_backplane.ui.routes.vault.writes.call_operation", _fake)
    monkeypatch.setattr("meho_backplane.ui.routes.vault.routes.call_operation", _fake)
    return calls


def _client(role: TenantRole = TenantRole.OPERATOR, sub: str = _OP_OPERATOR) -> Any:
    return _client_with_role_and_csrf(tenant_id=_TENANT_A, operator_sub=sub, role=role)


def _puts(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c["op_id"] == "vault.kv.put"]


def test_card_shows_the_ways_and_their_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    _patch_ops(monkeypatch)
    client, mock, _csrf = _client()
    with mock:
        response = client.get("/ui/connectors/web-1/credential")
    assert response.status_code == 200, response.text
    body = response.text
    assert f"secret/tenants/{_TENANT_A}/web-1" in body
    assert "version 3" in body
    assert "Replace credential" in body
    assert "Private key" in body
    assert "Password" in body
    assert "Key text" in body
    assert "The text of the private key." in body
    assert 'name="field__key__username"' in body
    assert 'name="field__password__password"' in body
    # A hidden single-line field is a password box with no autocomplete.
    assert 'type="password"' in body
    assert 'autocomplete="new-password"' in body


def test_a_several_lines_field_is_a_hidden_text_area_with_a_file_pick() -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    client, mock, _csrf = _client()
    with mock, pytest.MonkeyPatch.context() as mp:
        _patch_ops(mp, versions=_fault("InvalidPath"))
        response = client.get("/ui/connectors/web-1/credential")
    body = response.text
    assert "<textarea" in body
    assert "-webkit-text-security: disc" in body
    assert "data-credential-multiline" in body
    assert 'type="file"' in body
    assert 'data-credential-file-for="cred-key-private_key"' in body
    assert "Store credential" in body
    assert "No credential is stored yet." in body


def test_submit_sends_put_with_only_the_chosen_way(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    calls = _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock:
        response = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data={
                "way": "password",
                "field__password__username": "svc",
                "field__password__password": _SECRET_VALUE,
                # A field of the other way is ignored.
                "field__key__private_key": "other-way-text",
            },
        )
    assert response.status_code == 200, response.text
    (put,) = _puts(calls)
    assert put["connector_id"].startswith("vault-")
    assert put["params"] == {
        "mount": "secret",
        "path": f"tenants/{_TENANT_A}/web-1",
        "data": {"username": "svc", "password": _SECRET_VALUE},
    }


def test_the_answer_never_holds_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock, structlog.testing.capture_logs() as logs:
        ok = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data={
                "way": "password",
                "field__password__username": "svc",
                "field__password__password": _SECRET_VALUE,
            },
        )
        # A missing required field (the key way: its key text is empty).
        bad = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data={
                "way": "key",
                "field__key__username": _SECRET_VALUE,
                "field__key__private_key": "",
            },
        )
    assert ok.status_code == 200
    assert bad.status_code == 422
    assert "This field is required." in bad.text
    assert _SECRET_VALUE not in ok.text
    assert _SECRET_VALUE not in bad.text
    assert _SECRET_VALUE not in str(logs)


def test_unknown_way_is_refused_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    calls = _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock:
        response = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data={"way": "nope", "field__password__password": "x"},
        )
    assert response.status_code == 422
    assert "Choose how MEHO signs in" in response.text
    assert _puts(calls) == []


def test_no_declaration_shows_no_form_and_the_guide_link(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "old-1", product="cliprod")
    calls = _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock:
        card = client.get("/ui/connectors/old-1/credential")
        post = client.post(
            "/ui/connectors/old-1/credential",
            headers=_csrf_headers(csrf),
            data=_PASSWORD_FORM,
        )
    assert "This connector is set up with the command line for now" in card.text
    assert "guides/targets-and-secrets/#store-the-credential-through-meho" in card.text
    assert "<form" not in card.text
    assert post.status_code == 409
    assert _puts(calls) == []


def test_declared_no_credential_shows_no_form(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "free-1", product="freeprod")
    _patch_ops(monkeypatch)
    client, mock, _csrf = _client()
    with mock:
        card = client.get("/ui/connectors/free-1/credential")
    assert "This connector needs no credential" in card.text
    assert "<form" not in card.text


def test_admin_gets_approve_now_and_operator_gets_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    envelope = _awaiting_envelope("vault.kv.put")
    request_id = envelope["extras"]["approval_request_id"]
    _patch_ops(monkeypatch, put=envelope)
    data = _PASSWORD_FORM
    admin, mock, csrf = _client(TenantRole.TENANT_ADMIN, _OP_ADMIN)
    with mock:
        as_admin = admin.post(
            "/ui/connectors/web-1/credential", headers=_csrf_headers(csrf), data=data
        )
    operator, mock2, csrf2 = _client()
    with mock2:
        as_operator = operator.post(
            "/ui/connectors/web-1/credential", headers=_csrf_headers(csrf2), data=data
        )
    assert 'data-credential-result="awaiting_approval"' in as_admin.text
    assert "data-credential-approve-now" in as_admin.text
    assert f"/ui/approvals/{request_id}" in as_admin.text
    assert request_id in as_admin.text
    assert "An Admin of this tenant must approve this" not in as_admin.text
    assert "data-credential-approve-now" not in as_operator.text
    assert "An Admin of this tenant must approve this" in as_operator.text
    assert f"/ui/approvals/{request_id}" in as_operator.text


def test_a_path_outside_the_tenant_folder_is_refused_with_the_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1", secret_ref=f"tenants/{_TENANT_B}/web-1")
    _patch_ops(
        monkeypatch, versions=_fault("VaultTenantScopeError"), put=_fault("VaultTenantScopeError")
    )
    client, mock, csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
        post = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data=_PASSWORD_FORM,
        )
    assert "Outside your tenant scope" in card.text
    assert "Outside your tenant scope" in post.text
    assert "MEHO cannot tell if a credential is stored." in card.text


def test_a_failed_status_read_is_not_shown_as_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    _patch_ops(monkeypatch, versions=_fault("Forbidden"))
    client, mock, _csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
    assert "MEHO cannot tell if a credential is stored." in card.text
    assert "No credential is stored yet." not in card.text


def test_a_deleted_version_counts_as_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    gone = _versions_ok()
    gone["result"]["versions"]["3"]["deletion_time"] = "2026-11-21T10:00:00Z"
    _patch_ops(monkeypatch, versions=gone)
    client, mock, _csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
    assert "No credential is stored yet." in card.text


def test_gsm_install_shows_the_secret_name_and_no_form(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1", secret_ref="gsm:proj-x/web-1-cred")
    calls = _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
        post = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data=_PASSWORD_FORM,
        )
    assert "MEHO cannot store credentials yet" in card.text
    assert "proj-x/web-1-cred" in card.text
    assert "<form" not in card.text
    assert post.status_code == 409
    assert calls == []


def test_gsm_default_backend_with_a_plain_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1", secret_ref="web-1-cred")
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    get_settings.cache_clear()
    _patch_ops(monkeypatch)
    client, mock, _csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
    assert "MEHO cannot store credentials yet" in card.text
    assert "web-1-cred" in card.text
    assert "<form" not in card.text


def test_post_without_the_csrf_token_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    calls = _patch_ops(monkeypatch)
    client, mock, _csrf = _client()
    with mock:
        response = client.post(
            "/ui/connectors/web-1/credential",
            headers={"HX-Request": "true"},
            data=_PASSWORD_FORM,
        )
    assert response.status_code == 403
    assert _puts(calls) == []


def test_read_only_gets_403_on_the_card_and_the_post(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    calls = _patch_ops(monkeypatch)
    client, mock, csrf = _client(TenantRole.READ_ONLY, _OP_READ_ONLY)
    with mock:
        card = client.get("/ui/connectors/web-1/credential")
        post = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data=_PASSWORD_FORM,
        )
    assert card.status_code == 403
    assert post.status_code == 403
    assert calls == []


def test_a_target_of_another_tenant_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup()
    _seed_tenant(_TENANT_B, "tenant-b")
    _seed_target(_TENANT_B, "web-b")
    _patch_ops(monkeypatch)
    client, mock, csrf = _client()
    with mock:
        card = client.get("/ui/connectors/web-b/credential")
        post = client.post(
            "/ui/connectors/web-b/credential",
            headers=_csrf_headers(csrf),
            data=_PASSWORD_FORM,
        )
    assert card.status_code == 404
    assert post.status_code == 404


def test_the_detail_page_loads_the_card_and_the_script() -> None:
    _setup()
    _seed_target(_TENANT_A, "web-1")
    client, mock, _csrf = _client()
    with mock:
        page = client.get("/ui/connectors/web-1")
    assert 'hx-get="/ui/connectors/web-1/credential"' in page.text
    assert "credential-card.js" in page.text


def test_real_dispatch_waits_for_approval_and_leaks_no_value() -> None:
    """The real dispatch parks the write. The value reaches no audit row, log line or page."""
    _setup()
    _seed_target(_TENANT_A, "web-1")
    client, mock, csrf = _client()
    with pytest.MonkeyPatch.context() as mp, mock, structlog.testing.capture_logs() as logs:
        fake = install_fake_client(mp, secret={"password": "old"})
        fake.sys.default_capabilities = ["create", "read", "update"]
        response = client.post(
            "/ui/connectors/web-1/credential",
            headers=_csrf_headers(csrf),
            data={
                "way": "password",
                "field__password__username": "svc",
                "field__password__password": _SECRET_VALUE,
            },
        )
    assert response.status_code == 200, response.text
    assert 'data-credential-result="awaiting_approval"' in response.text
    assert _SECRET_VALUE not in response.text
    assert _SECRET_VALUE not in str(logs)

    async def _rows() -> tuple[list[AuditLog], list[ApprovalRequest]]:
        async with get_sessionmaker()() as session:
            audit = list((await session.execute(select(AuditLog))).scalars())
            parked = list((await session.execute(select(ApprovalRequest))).scalars())
            return audit, parked

    audit, parked = asyncio.run(_rows())
    assert len(parked) == 1
    for row in audit:
        assert _SECRET_VALUE not in str(row.__dict__)
