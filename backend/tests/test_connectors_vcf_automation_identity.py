# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VCFA sign-in ops: the provider's LDAP and each org's LDAP and OIDC.

Drives each op through the real dispatcher against a respx-mocked appliance
(made-up names and ids) and pins: the risk levels; the read as flat rows with
no password or client secret, even when the vendor sends one; the classic
``/api`` ``Accept`` header (the cloudapi header answers 406); an LDAP server
that cannot be reached; a wrong bind password (refused, nothing changes, the
certificate this run trusted is removed again); an untrusted LDAPS certificate
(its fingerprint is shown and it is trusted only when the call names it); the
OIDC well-known address that does not answer and a refused client secret; the
refusal to turn off the provider's own org; the approval wait; the previews;
and that no secret reaches an answer, the audit row or an error.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.vcf_automation import (
    VCFA_IDENTITY_OPS,
    VcfAutomationConnector,
)
from meho_backplane.connectors.vcf_automation import _identity as identity_module
from meho_backplane.connectors.vcf_automation._identity_ldap import certificate_sha256
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    PassThroughReducer,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.operations._vendor_text import VENDOR_TEXT_WITHHELD, sends_secret
from tests._vcfa_provisioning_support import (
    BASE_URL,
    OPERATOR,
    ORG,
    ORG_UUID,
    mount_logins,
    page,
    resolved_target,
    run,
    seed_target,
    wire_connector,
)

_ORGS = "/cloudapi/1.0.0/orgs"
_PROVIDER_LDAP = "/api/admin/extension/settings/ldapSettings"
_ORG_LDAP = f"/api/admin/org/{ORG_UUID}/settings/ldap"
_ORG_OAUTH = f"/api/admin/org/{ORG_UUID}/settings/oauth"
_ORG_OIDC_CONFIG = f"{_ORG_OAUTH}/openIdProviderConfig"
_TEST_CONNECTION = "/cloudapi/1.0.0/testConnection"
_TRUSTED = "/cloudapi/1.0.0/ssl/trustedCertificates"
_CLASSIC_ACCEPT = "application/*+json;version=40.0"

#: The secrets the stubbed Vault read returns. They must never reach a result.
BIND_PASSWORD = "Pw-sentinel-bind"
CLIENT_SECRET = "Cs-sentinel-oidc"

SYSTEM_ORG = {"id": "urn:vcloud:org:99999999-0000-0000-0000-000000000001", "name": "System"}

_LDAP_PARAMS: dict[str, Any] = {
    "host": "ldap.example.invalid",
    "search_base": "dc=example,dc=invalid",
    "bind_user": "cn=bind,dc=example,dc=invalid",
    "password_secret_ref": "example/ldap-bind",
    "connector_type": "ACTIVE_DIRECTORY",
}
_OIDC_PARAMS: dict[str, Any] = {
    "org": "example-org",
    "well_known_endpoint": "https://idp.example.invalid/.well-known/openid-configuration",
    "client_id": "vcfa-client",
    "client_secret_ref": "example/oidc-client",
    "claims": {"subject": "preferred_username"},
}


def _self_signed_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ldap.example.invalid")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CERT_PEM = _self_signed_pem()
CERT_SHA256 = (certificate_sha256(CERT_PEM) or ("", ""))[1]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    reset_dispatcher_caches()
    reset_handler_cache()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Stub the Vault read of ``*_secret_ref``; record ``(param, ref)`` per read."""
    reads: list[tuple[str, str]] = []

    async def _load(target: Any, _operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        reads.append((param, str(target.secret_ref)))
        return {"password": BIND_PASSWORD, "client_secret": CLIENT_SECRET}

    monkeypatch.setattr(identity_module, "load_param_secret_data", _load)
    return reads


@pytest.fixture
async def vcfa() -> AsyncIterator[VcfAutomationConnector]:
    set_default_reducer(PassThroughReducer())
    await VcfAutomationConnector.register_typed_operations()
    await seed_target()
    connector = wire_connector()
    yield connector
    await connector.aclose()


def _classic(payload: dict[str, Any]) -> Any:
    """A side effect that answers like the classic ``/api``: 406 on a wrong ``Accept``."""

    def _respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Accept") != _CLASSIC_ACCEPT:
            return httpx.Response(406, json={"message": "not acceptable"})
        return httpx.Response(200, json=payload)

    return _respond


def _probe(*, can_connect: bool = True, ssl_result: str | None = None) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "canConnect": can_connect,
        "connectionResult": "SUCCESS" if can_connect else "ERROR_CANNOT_CONNECT",
        "result": "ok" if can_connect else "Cannot connect",
        "sslResult": ssl_result,
    }
    if ssl_result == "ERROR_UNTRUSTED_CERTIFICATE":
        probe["certificateChain"] = CERT_PEM
    return {"targetProbe": probe}


def _ldap_answer(**extra: Any) -> dict[str, Any]:
    return {
        "hostName": "ldap.example.invalid",
        "port": 389,
        "isSsl": False,
        "searchBase": "dc=example,dc=invalid",
        "userName": "cn=bind,dc=example,dc=invalid",
        "connectorType": "ACTIVE_DIRECTORY",
        "authenticationMechanism": "SIMPLE",
        # The vendor sends the password back; MEHO must drop it.
        "password": BIND_PASSWORD,
        **extra,
    }


def _oauth_answer() -> dict[str, Any]:
    return {
        "enabled": True,
        "wellKnownEndpoint": _OIDC_PARAMS["well_known_endpoint"],
        "issuerId": "https://idp.example.invalid",
        "clientId": "vcfa-client",
        "clientSecret": CLIENT_SECRET,
        "scope": ["openid", "profile", "email"],
        "OIDCAttributeMapping": {
            "subjectAttributeName": "preferred_username",
            "emailAttributeName": "email",
            "fullNameAttributeName": "name",
        },
    }


def _found_settings() -> dict[str, Any]:
    """What the appliance read from the well-known address."""
    return {
        "orgOAuthSettings": {
            "issuerId": "https://idp.example.invalid",
            "userAuthorizationEndpoint": "https://idp.example.invalid/auth",
            "accessTokenEndpoint": "https://idp.example.invalid/token",
            "userInfoEndpoint": "https://idp.example.invalid/userinfo",
            "jwksUri": "https://idp.example.invalid/jwks",
            "scope": ["openid"],
            "OIDCAttributeMapping": {
                "subjectAttributeName": "sub",
                "emailAttributeName": "email",
                "fullNameAttributeName": "name",
            },
        }
    }


def _mount_orgs(mock: respx.MockRouter, orgs: list[dict[str, Any]] | None = None) -> None:
    mock.get(_ORGS).respond(200, json=page(orgs if orgs is not None else [ORG]))


def _body(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


def _no_secret(value: Any) -> None:
    text = json.dumps(value, default=str)
    assert BIND_PASSWORD not in text
    assert CLIENT_SECRET not in text


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_identity_risk_levels() -> None:
    assert {op.op_id: (op.safety_level, op.requires_approval) for op in VCFA_IDENTITY_OPS} == {
        "vcfa.provider.identity.show": ("safe", False),
        "vcfa.provider.ldap.set": ("dangerous", True),
        "vcfa.provider.org.ldap.set": ("dangerous", True),
        "vcfa.provider.org.oidc.set": ("dangerous", True),
    }


@pytest.mark.parametrize(
    "op_id",
    ["vcfa.provider.ldap.set", "vcfa.provider.org.ldap.set", "vcfa.provider.org.oidc.set"],
)
def test_changes_are_credential_writes(op_id: str) -> None:
    op = next(op for op in VCFA_IDENTITY_OPS if op.op_id == op_id)
    assert "credential" in op.tags
    assert classify_op(op_id) == "credential_write"
    assert sends_secret(op_id, op.tags)
    secret_params = [name for name in op.parameter_schema["properties"] if "secret" in name]
    assert all(name.endswith(("_ref", "_mount", "_key")) for name in secret_params)


async def test_identity_ops_register_as_typed_rows(vcfa: VcfAutomationConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id.in_([op.op_id for op in VCFA_IDENTITY_OPS])
                )
            )
        ).scalars()
        by_op = {row.op_id: row for row in rows}
    assert set(by_op) == {op.op_id for op in VCFA_IDENTITY_OPS}
    assert all(row.source_kind == "typed" for row in by_op.values())


# ---------------------------------------------------------------------------
# identity.show
# ---------------------------------------------------------------------------


async def test_show_answers_flat_rows_without_secrets(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        provider = mock.get(_PROVIDER_LDAP).mock(side_effect=_classic(_ldap_answer()))
        org_ldap = mock.get(_ORG_LDAP).mock(
            side_effect=_classic(
                {"orgLdapMode": "CUSTOM", "customOrgLdapSettings": _ldap_answer(isSsl=True)}
            )
        )
        mock.get(_ORG_OAUTH).mock(side_effect=_classic(_oauth_answer()))
        result = await run("vcfa.provider.identity.show", {})
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["org_count"] == 1
    rows = {(row["applies_to"], row["kind"]): row for row in out["rows"]}
    assert rows[("provider", "ldap")]["ldap_mode"] == "CUSTOM"
    assert rows[("provider", "ldap")]["bind_user"] == "cn=bind,dc=example,dc=invalid"
    assert rows[("org", "ldap")]["is_ssl"] is True
    assert rows[("org", "ldap")]["org"] == "example-org"
    oidc = rows[("org", "oidc")]
    assert oidc["client_id"] == "vcfa-client"
    assert oidc["claim_subject"] == "preferred_username"
    assert oidc["scope"] == "openid profile email"
    _no_secret(result)
    for route in (provider, org_ldap):
        assert route.calls.last.request.headers["Accept"] == _CLASSIC_ACCEPT


async def test_show_one_org_keeps_going_after_a_refused_read(
    vcfa: VcfAutomationConnector,
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        provider = mock.get(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        mock.get(_ORG_LDAP).respond(200, json={"orgLdapMode": "SYSTEM"})
        mock.get(_ORG_OAUTH).respond(403, json={"message": "no access"})
        result = await run("vcfa.provider.identity.show", {"org": "example-org"})
    rows = result["result"]["rows"]
    assert [(row["kind"], row["error"]) for row in rows] == [
        ("ldap", None),
        ("oidc", "HTTP 403: no access"),
    ]
    assert rows[0]["ldap_mode"] == "SYSTEM"
    assert not provider.called


# ---------------------------------------------------------------------------
# ldap.set
# ---------------------------------------------------------------------------


async def test_ldap_set_waits_for_approval(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        put = mock.put(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        result = await run("vcfa.provider.ldap.set", _LDAP_PARAMS, approved=False)
    assert result["status"] != "ok"
    assert "approval" in json.dumps(result).lower()
    assert not put.called
    assert vault == []


async def test_ldap_set_sets_the_provider_ldap(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        probe = mock.post(_TEST_CONNECTION).respond(200, json=_probe())
        put = mock.put(_PROVIDER_LDAP).mock(side_effect=_classic(_ldap_answer()))
        result = await run("vcfa.provider.ldap.set", _LDAP_PARAMS)
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "set"
    assert out["settings"]["host"] == "ldap.example.invalid"
    assert out["trusted_certificate_id"] is None
    body = _body(put)
    assert body["password"] == BIND_PASSWORD
    assert body["port"] == 389
    assert body["userAttributes"]["userName"] == "sAMAccountName"
    assert body["groupAttributes"]["membership"] == "member"
    assert put.calls.last.request.headers["Content-Type"] == "application/*+json"
    assert _body(probe) == {
        "host": "ldap.example.invalid",
        "port": 389,
        "secure": False,
        "hostnameVerificationAlgorithm": "LDAPS",
        "timeout": 30,
    }
    assert vault == [("password_secret_ref", "example/ldap-bind")]
    _no_secret(result)


async def test_ldap_set_unreachable_server_changes_nothing(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(200, json=_probe(can_connect=False))
        put = mock.put(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        result = await run("vcfa.provider.ldap.set", _LDAP_PARAMS)
    assert result["result"]["status"] == "unreachable"
    assert "Cannot connect" in result["result"]["guidance"]
    assert not put.called
    assert vault == []


async def test_ldap_set_wrong_bind_password_is_refused_and_hidden(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(200, json=_probe())
        mock.put(_PROVIDER_LDAP).respond(
            400, json={"message": f"Bind failed for password {BIND_PASSWORD}"}
        )
        result = await run("vcfa.provider.ldap.set", _LDAP_PARAMS)
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "refused"
    assert out["vendor_message"].startswith("HTTP 400: [MEHO hid this text")
    assert "old settings stay" in out["guidance"]
    _no_secret(result)
    async with get_sessionmaker()() as session:
        audits = [
            row.payload
            for row in (await session.execute(select(AuditLog))).scalars().all()
            if row.payload.get("op_id") == "vcfa.provider.ldap.set"
        ]
    assert audits
    _no_secret(audits)


async def test_ldap_set_vendor_error_text_is_withheld(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    """A raised vendor error keeps its status but not the vendor's text."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(500, json={"message": "probe broke"})
        result = await run("vcfa.provider.ldap.set", _LDAP_PARAMS)
    assert result["status"] == "error", result
    assert "probe broke" not in json.dumps(result)
    assert VENDOR_TEXT_WITHHELD in json.dumps(result)


async def test_ldaps_untrusted_certificate_shows_the_fingerprint(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(
            200, json=_probe(ssl_result="ERROR_UNTRUSTED_CERTIFICATE")
        )
        trust = mock.post(_TRUSTED).respond(201, json={"id": "urn:vcloud:cert:1"})
        put = mock.put(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        result = await run("vcfa.provider.ldap.set", {**_LDAP_PARAMS, "is_ssl": True})
    out = result["result"]
    assert out["status"] == "certificate_not_trusted"
    assert out["certificate_sha256"] == CERT_SHA256
    assert "trust_certificate_sha256" in out["guidance"]
    assert not trust.called
    assert not put.called
    assert vault == []


async def test_ldaps_certificate_is_trusted_when_the_call_names_it(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    params = {**_LDAP_PARAMS, "is_ssl": True, "trust_certificate_sha256": CERT_SHA256.lower()}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(
            200, json=_probe(ssl_result="ERROR_UNTRUSTED_CERTIFICATE")
        )
        trust = mock.post(_TRUSTED).respond(201, json={"id": "urn:vcloud:cert:1"})
        put = mock.put(_PROVIDER_LDAP).respond(200, json=_ldap_answer(isSsl=True, port=636))
        result = await run("vcfa.provider.ldap.set", params)
    out = result["result"]
    assert out["status"] == "set", out
    assert out["trusted_certificate_id"] == "urn:vcloud:cert:1"
    assert _body(trust)["certificate"].strip() == CERT_PEM.strip()
    assert _body(trust)["alias"].startswith("ldap.example.invalid_")
    assert _body(put)["port"] == 636


async def test_ldaps_other_certificate_is_not_trusted(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    params = {**_LDAP_PARAMS, "is_ssl": True, "trust_certificate_sha256": "AB" * 32}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(
            200, json=_probe(ssl_result="ERROR_UNTRUSTED_CERTIFICATE")
        )
        trust = mock.post(_TRUSTED).respond(201, json={"id": "urn:vcloud:cert:1"})
        put = mock.put(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        result = await run("vcfa.provider.ldap.set", params)
    assert result["result"]["status"] == "certificate_mismatch"
    assert result["result"]["certificate_sha256"] == CERT_SHA256
    assert not trust.called
    assert not put.called


async def test_ldaps_refused_set_removes_the_trusted_certificate(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    params = {**_LDAP_PARAMS, "is_ssl": True, "trust_certificate_sha256": CERT_SHA256}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.post(_TEST_CONNECTION).respond(
            200, json=_probe(ssl_result="ERROR_UNTRUSTED_CERTIFICATE")
        )
        mock.post(_TRUSTED).respond(201, json={"id": "urn:vcloud:cert:1"})
        undo = mock.delete(f"{_TRUSTED}/urn%3Avcloud%3Acert%3A1").respond(204)
        mock.put(_PROVIDER_LDAP).respond(400, json={"message": "Invalid credentials"})
        result = await run("vcfa.provider.ldap.set", params)
    out = result["result"]
    assert out["status"] == "refused"
    assert out["vendor_message"] == "HTTP 400: Invalid credentials"
    assert undo.called
    assert "could not remove" not in out["guidance"]


async def test_ldap_set_preview_shows_the_vault_path_and_fingerprint(
    vcfa: VcfAutomationConnector,
) -> None:
    params = {**_LDAP_PARAMS, "is_ssl": True, "trust_certificate_sha256": CERT_SHA256}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        mock.get(_PROVIDER_LDAP).respond(200, json=_ldap_answer())
        mock.post(_TEST_CONNECTION).respond(
            200, json=_probe(ssl_result="ERROR_UNTRUSTED_CERTIFICATE")
        )
        trust = mock.post(_TRUSTED).respond(201, json={})
        put = mock.put(_PROVIDER_LDAP).respond(200, json={})
        parked = await _preview("vcfa.provider.ldap.set", params, vcfa)
    egress_free = await _preview("vcfa.provider.ldap.set", params, None)
    assert parked is not None and egress_free is not None
    preview = parked["preview"]
    assert preview["password_from_vault_path"] == "example/ldap-bind"
    assert preview["certificate_sha256"] == CERT_SHA256
    assert preview["certificate_matches"] is True
    assert preview["current"]["host"] == "ldap.example.invalid"
    assert egress_free["preview"]["current"] is None
    assert not trust.called
    assert not put.called
    _no_secret(parked)


# ---------------------------------------------------------------------------
# org.ldap.set
# ---------------------------------------------------------------------------


async def test_org_ldap_set_system_mode(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        put = mock.put(_ORG_LDAP).mock(side_effect=_classic({"orgLdapMode": "SYSTEM"}))
        result = await run(
            "vcfa.provider.org.ldap.set",
            {"org": "example-org", "mode": "system", "custom_users_ou": "ou=example"},
        )
    assert result["result"]["status"] == "set", result
    assert result["result"]["settings"]["ldap_mode"] == "SYSTEM"
    assert _body(put) == {"orgLdapMode": "SYSTEM", "customUsersOu": "ou=example"}


async def test_org_ldap_set_custom_sends_the_server_with_the_password(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    params = {"org": "example-org", "mode": "custom", **_LDAP_PARAMS}
    answer = {"orgLdapMode": "CUSTOM", "customOrgLdapSettings": _ldap_answer()}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        mock.post(_TEST_CONNECTION).respond(200, json=_probe())
        put = mock.put(_ORG_LDAP).respond(200, json=answer)
        result = await run("vcfa.provider.org.ldap.set", params)
    assert result["result"]["status"] == "set", result
    body = _body(put)
    assert body["orgLdapMode"] == "CUSTOM"
    assert body["customOrgLdapSettings"]["password"] == BIND_PASSWORD
    assert body["customOrgLdapSettings"]["hostName"] == "ldap.example.invalid"
    _no_secret(result)


@pytest.mark.parametrize("mode", ["none", "system"])
async def test_org_ldap_set_refuses_the_provider_org(
    vcfa: VcfAutomationConnector, mode: str
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock, [SYSTEM_ORG])
        put = mock.put(url__regex=r"/api/admin/org/.*").respond(200, json={})
        result = await run("vcfa.provider.org.ldap.set", {"org": "System", "mode": mode})
    assert result["result"]["status"] == "refused_by_meho"
    assert "provider admin signs in" in result["result"]["guidance"]
    assert not put.called


async def test_org_ldap_set_openldap_needs_the_attribute_maps(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    params = {"org": "example-org", "mode": "custom", **_LDAP_PARAMS, "connector_type": "OPENLDAP"}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        put = mock.put(_ORG_LDAP).respond(200, json={})
        result = await run("vcfa.provider.org.ldap.set", params)
    assert result["result"]["status"] == "invalid_request"
    assert "user_attributes" in result["result"]["guidance"]
    assert not put.called


# ---------------------------------------------------------------------------
# org.oidc.set
# ---------------------------------------------------------------------------


async def test_oidc_set_reads_the_well_known_and_sets_the_secret(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        config = mock.post(_ORG_OIDC_CONFIG).mock(side_effect=_classic(_found_settings()))
        put = mock.put(_ORG_OAUTH).respond(200, json={})
        mock.get(_ORG_OAUTH).respond(200, json=_oauth_answer())
        result = await run("vcfa.provider.org.oidc.set", _OIDC_PARAMS)
    out = result["result"]
    assert out["status"] == "set", out
    assert out["settings"]["client_id"] == "vcfa-client"
    assert _body(config) == {
        "openIdProviderConfigurationEndpoint": _OIDC_PARAMS["well_known_endpoint"]
    }
    body = _body(put)
    assert body["clientSecret"] == CLIENT_SECRET
    assert body["clientId"] == "vcfa-client"
    assert body["enabled"] is True
    assert body["accessTokenEndpoint"] == "https://idp.example.invalid/token"
    assert body["OIDCAttributeMapping"]["subjectAttributeName"] == "preferred_username"
    assert body["OIDCAttributeMapping"]["emailAttributeName"] == "email"
    assert vault == [("client_secret_ref", "example/oidc-client")]
    _no_secret(result)


async def test_oidc_set_well_known_that_does_not_answer(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        mock.post(_ORG_OIDC_CONFIG).respond(
            400, json={"message": "Unable to reach the OpenID provider"}
        )
        put = mock.put(_ORG_OAUTH).respond(200, json={})
        result = await run("vcfa.provider.org.oidc.set", _OIDC_PARAMS)
    out = result["result"]
    assert out["status"] == "well_known_failed"
    assert out["vendor_message"] == "HTTP 400: Unable to reach the OpenID provider"
    assert not put.called
    assert vault == []


async def test_oidc_set_wrong_client_secret_is_refused_and_hidden(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        mock.post(_ORG_OIDC_CONFIG).respond(200, json=_found_settings())
        mock.put(_ORG_OAUTH).respond(
            400, json={"message": f"invalid_client: secret {CLIENT_SECRET} rejected"}
        )
        result = await run("vcfa.provider.org.oidc.set", _OIDC_PARAMS)
    assert result["result"]["status"] == "refused"
    assert "MEHO hid this text" in result["result"]["vendor_message"]
    _no_secret(result)


async def test_oidc_set_names_missing_claims(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    found = {"orgOAuthSettings": {"userAuthorizationEndpoint": "https://idp.example.invalid/a"}}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        mock.post(_ORG_OIDC_CONFIG).respond(200, json=found)
        put = mock.put(_ORG_OAUTH).respond(200, json={})
        result = await run("vcfa.provider.org.oidc.set", _OIDC_PARAMS)
    assert result["status"] == "ok", result
    out = result["result"]
    assert out["status"] == "invalid_request"
    assert "accessTokenEndpoint" in out["guidance"]
    assert "claims.email" in out["guidance"]
    assert not put.called


async def test_oidc_remove(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        delete = mock.delete(_ORG_OAUTH).respond(204)
        result = await run("vcfa.provider.org.oidc.set", {"org": "example-org", "state": "absent"})
    assert result["result"]["status"] == "removed", result
    assert delete.called


async def test_oidc_remove_refuses_the_provider_org(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock, [SYSTEM_ORG])
        delete = mock.delete(url__regex=r"/api/admin/org/.*").respond(204)
        result = await run("vcfa.provider.org.oidc.set", {"org": "System", "state": "absent"})
    assert result["result"]["status"] == "refused_by_meho"
    assert not delete.called


async def test_oidc_turn_off_refuses_the_provider_org(
    vcfa: VcfAutomationConnector, vault: list[tuple[str, str]]
) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock, [SYSTEM_ORG])
        post = mock.post(url__regex=r"/api/admin/org/.*").respond(200, json={})
        put = mock.put(url__regex=r"/api/admin/org/.*").respond(200, json={})
        result = await run(
            "vcfa.provider.org.oidc.set", {**_OIDC_PARAMS, "org": "System", "enabled": False}
        )
    assert result["result"]["status"] == "refused_by_meho"
    assert "remove or turn off" in result["result"]["guidance"]
    assert not post.called
    assert not put.called
    assert vault == []


async def test_oidc_set_preview_shows_only_the_vault_path(vcfa: VcfAutomationConnector) -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mount_logins(mock)
        _mount_orgs(mock)
        mock.get(_ORG_OAUTH).respond(200, json=_oauth_answer())
        parked = await _preview("vcfa.provider.org.oidc.set", _OIDC_PARAMS, vcfa)
    assert parked is not None
    preview = parked["preview"]
    assert preview["action"] == "set_org_oidc"
    assert preview["client_secret_from_vault_path"] == "example/oidc-client"
    assert preview["org_id"] == ORG["id"]
    assert preview["current"]["client_id"] == "vcfa-client"
    _no_secret(parked)


async def _preview(op_id: str, params: dict[str, Any], connector: Any) -> dict[str, Any] | None:
    async with get_sessionmaker()() as session:
        descriptor = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    return await build_proposed_effect(
        PreviewContext(
            descriptor=descriptor,
            connector_instance=connector,
            operator=OPERATOR,
            target=await resolved_target(),
            params=params,
            connector_id="vcfa-rest-9.0",
        )
    )
