# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the SRM appliance certificates and the storage replication adapter install.

What is covered, with a respx-mocked appliance setup API and archive
server, and certificates made in the test:

* ``srm.appliance.csr.create``: the request comes back with its subject
  and names; the DNS names go out comma-joined;
* ``srm.appliance.certificate.set``: a matching certificate is installed
  and the vendor task comes back (``accepted``); a certificate for another
  key, one without the appliance's name and one that has expired are
  refused before any change; a running certificate task is returned and
  nothing starts; a vendor error reaches the steps; CA certificates are
  added and removed;
* ``srm.storage_adapter.install``: a good run (download, SHA-256, upload,
  the new adapter in the list); an archive URL that does not answer, a
  wrong SHA-256, a redirect to another host and an upload that breaks off
  (nothing installed, and the answer says so); the vendor session kept
  alive during a slow download; the temporary folder removed every time
  and old leftovers removed; the URL query (a signed-URL token) in no
  answer; the preview with its blast radius, the preview that refuses a
  missing archive, and the call refused without a matching preview hash.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.srm import (
    SRM_CONNECTOR_ID,
    SRM_IMPL_ID,
    SRM_PRODUCT,
    SRM_VERSION,
    SrmConnector,
    register_srm_typed_operations,
)
from meho_backplane.connectors.srm import storage_adapter as sra
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

_HOST = "srm-certs.test.invalid"
_BASE_URL = f"https://{_HOST}"
_API = "/api/rest/configure/v2"
_ADMIN_REF = "<vault-path-of-appliance-admin>"
_SSO_REF = "<vault-path-of-sso-admin>"
_ADMIN_PASSWORD = "appliance-admin-made-up"
_SSO_PASSWORD = "sso-admin-made-up"
_APPLIANCE_FQDN = "srm-01.test.invalid"
_ARCHIVE_HOST = "downloads.test.invalid"
_ARCHIVE_PATH = "/sra/made-up-adapter.tar.gz"
_SIGNED_QUERY = "sig=made-up-signed-url-token"
_ARCHIVE_URL = f"https://{_ARCHIVE_HOST}{_ARCHIVE_PATH}?{_SIGNED_QUERY}"
_ARCHIVE = b"made-up adapter archive bytes " * 1000
_ARCHIVE_SHA = hashlib.sha256(_ARCHIVE).hexdigest()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SRM_PRODUCT, version=SRM_VERSION, impl_id=SRM_IMPL_ID, cls=SrmConnector
    )
    monkeypatch.setattr("meho_backplane.connectors.srm.setup_api.TASK_POLL_SECONDS", 0.0)
    # Temporary folders of the install go under this test's own folder.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return AsyncMock()


class _Target:
    def __init__(self) -> None:
        self.product = SRM_PRODUCT
        self.fingerprint = type("_FP", (), {"version": SRM_VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = f"srm-certs-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.fqdn: str | None = None
        self.secret_ref = "<vault-path-of-srm-login>"
        self.auth_model = "shared_service_account"


def _operator() -> Operator:
    return Operator(
        sub="op-srm-certs",
        name="SRM Certs Operator",
        email=None,
        raw_jwt="op.srm.certs.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: object, _operator: Operator, param: str, _ref: str) -> dict[str, str]:
    if param == "sso_secret_ref":
        return {"username": "sso-admin", "password": _SSO_PASSWORD}
    return {"username": "admin", "password": _ADMIN_PASSWORD}


async def _connector(_stub_embedding: AsyncMock) -> SrmConnector:
    await register_srm_typed_operations()
    instance = get_or_create_connector_instance(SrmConnector)
    assert isinstance(instance, SrmConnector)
    instance.setup_credentials_loader = _loader
    return instance


def _login(mock: respx.MockRouter) -> None:
    mock.post(f"{_API}/session").respond(200, json={"session_id": "setup-1"})


def _no_secret(body: Any) -> None:
    text = json.dumps(body, default=str)
    assert _ADMIN_PASSWORD not in text
    assert _SSO_PASSWORD not in text
    assert _SIGNED_QUERY not in text


# ---------------------------------------------------------------------------
# Certificates made in the test
# ---------------------------------------------------------------------------


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _csr(key: ec.EllipticCurvePrivateKey) -> str:
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(_name(_APPLIANCE_FQDN))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_APPLIANCE_FQDN)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def _cert(
    key: ec.EllipticCurvePrivateKey,
    *,
    dns: str = _APPLIANCE_FQDN,
    days: int = 365,
    ca: bool = False,
) -> str:
    now = datetime.now(UTC)
    issuer_key = _key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(dns))
        .issuer_name(_name("made-up-ca"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    cert = builder.sign(issuer_key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode()


# ---------------------------------------------------------------------------
# srm.appliance.csr.create
# ---------------------------------------------------------------------------


async def test_csr_create_returns_the_request_and_its_names(_stub_embedding: AsyncMock) -> None:
    connector = await _connector(_stub_embedding)
    csr_pem = _csr(_key())
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        route = mock.post(f"{_API}/appliance/certificates/actions/generate-csr").respond(
            200, json={"value": csr_pem}
        )
        body = await connector.appliance_csr_create(
            _operator(),
            _Target(),
            {
                "appliance_secret_ref": _ADMIN_REF,
                "common_name": _APPLIANCE_FQDN,
                "fqdns": [_APPLIANCE_FQDN, "srm-alias.test.invalid"],
            },
        )
    assert json.loads(route.calls[0].request.content) == {
        "common_name": _APPLIANCE_FQDN,
        "fqdn": f"{_APPLIANCE_FQDN},srm-alias.test.invalid",
    }
    assert body["csr_pem"] == csr_pem
    assert body["subject"] == f"CN={_APPLIANCE_FQDN}"
    assert body["subject_alternative_names"] == [_APPLIANCE_FQDN]
    assert len(body["public_key_sha256"]) == 64
    assert "PRIVATE KEY" not in json.dumps(body)


# ---------------------------------------------------------------------------
# srm.appliance.certificate.set
# ---------------------------------------------------------------------------


def _mock_cert(mock: respx.MockRouter, *, running: list[Any] | None = None) -> respx.Route:
    _login(mock)
    mock.get(f"{_API}/tasks").respond(200, json={"list": running or []})
    mock.get(f"{_API}/appliance").respond(200, json={"hostname": _APPLIANCE_FQDN})
    return mock.post(f"{_API}/appliance/certificates/server/actions/update").respond(
        202, json={"id": "task-cert", "status": "QUEUED", "type": "CERTIFICATE_UPDATE"}
    )


def _server_params(cert_pem: str, csr_pem: str) -> dict[str, Any]:
    return {
        "appliance_secret_ref": _ADMIN_REF,
        "sso_secret_ref": _SSO_REF,
        "server_certificate": {
            "certificate_pem": cert_pem,
            "csr_pem": csr_pem,
            "ca_chain_pem": [_cert(_key(), dns="made-up-ca", ca=True)],
        },
    }


async def test_matching_certificate_is_installed_and_the_task_returned(
    _stub_embedding: AsyncMock,
) -> None:
    connector = await _connector(_stub_embedding)
    key = _key()
    params = _server_params(_cert(key), _csr(key))
    async with respx.mock(base_url=_BASE_URL) as mock:
        update = _mock_cert(mock)
        body = await connector.appliance_certificate_set(_operator(), _Target(), params)
    assert body["outcome"] == "accepted", body
    assert body["task_id"] == "task-cert"
    assert body["status_read"]["op_id"] == "srm.appliance.task.get"
    sent = json.loads(update.calls[0].request.content)
    assert sent["csr_format"]["certificate"] == params["server_certificate"]["certificate_pem"]
    assert len(sent["csr_format"]["ca_chain"]) == 1
    assert sent["credentials"]["admin_user"] == "sso-admin"
    assert body["certificate"]["subject"] == f"CN={_APPLIANCE_FQDN}"
    assert "BEGIN CERTIFICATE" not in json.dumps(body)
    _no_secret(body)


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda key: (_cert(_key()), _csr(key)), "public key of the request"),
        (lambda key: (_cert(key, dns="other.test.invalid"), _csr(key)), "does not name"),
        (lambda key: (_cert(key, days=-1), _csr(key)), "expired"),
    ],
)
async def test_certificate_that_does_not_fit_is_refused(
    _stub_embedding: AsyncMock, make: Any, reason: str
) -> None:
    connector = await _connector(_stub_embedding)
    cert_pem, csr_pem = make(_key())
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        update = _mock_cert(mock)
        body = await connector.appliance_certificate_set(
            _operator(), _Target(), _server_params(cert_pem, csr_pem)
        )
    assert body["outcome"] == "refused"
    assert update.call_count == 0
    failed = next(row for row in body["steps"] if row["status"] == "failed")
    assert failed["step"] == "check_certificate"
    assert reason in failed["error"]


async def test_running_certificate_task_is_returned_and_nothing_starts(
    _stub_embedding: AsyncMock,
) -> None:
    connector = await _connector(_stub_embedding)
    key = _key()
    running = [{"id": "task-old", "status": "RUNNING", "type": "CERTIFICATE_UPDATE"}]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        update = _mock_cert(mock, running=running)
        body = await connector.appliance_certificate_set(
            _operator(), _Target(), _server_params(_cert(key), _csr(key))
        )
    assert body["outcome"] == "already_running"
    assert body["task_id"] == "task-old"
    assert update.call_count == 0


async def test_certificate_vendor_error_reaches_the_steps(_stub_embedding: AsyncMock) -> None:
    connector = await _connector(_stub_embedding)
    key = _key()
    async with respx.mock(base_url=_BASE_URL) as mock:
        update = _mock_cert(mock)
        update.respond(400, json={"message": "chain is incomplete"})
        body = await connector.appliance_certificate_set(
            _operator(), _Target(), _server_params(_cert(key), _csr(key))
        )
    assert body["outcome"] == "failed"
    assert [row["status"] for row in body["steps"]] == ["done", "done", "failed"]
    assert "chain is incomplete" in body["steps"][2]["error"]
    _no_secret(body)


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("add_ca_certificates", "/appliance/certificates/ca"),
        ("remove_ca_certificates", "/appliance/certificates/ca/actions/remove"),
    ],
)
async def test_ca_certificates_are_added_and_removed(
    _stub_embedding: AsyncMock, field: str, path: str
) -> None:
    connector = await _connector(_stub_embedding)
    pem = _cert(_key(), dns="made-up-ca", ca=True)
    async with respx.mock(base_url=_BASE_URL) as mock:
        _login(mock)
        route = mock.post(f"{_API}{path}").respond(204)
        body = await connector.appliance_certificate_set(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF, field: [pem]}
        )
    assert json.loads(route.calls[0].request.content) == [{"value": pem}]
    assert body["outcome"] == "done"
    assert body["change"] == field
    assert body["certificates"][0]["subject"] == "CN=made-up-ca"


async def test_certificate_set_needs_exactly_one_change(_stub_embedding: AsyncMock) -> None:
    connector = await _connector(_stub_embedding)
    with pytest.raises(ValueError, match="exactly one"):
        await connector.appliance_certificate_set(
            _operator(), _Target(), {"appliance_secret_ref": _ADMIN_REF}
        )


# ---------------------------------------------------------------------------
# srm.storage_adapter.install
# ---------------------------------------------------------------------------

_ADAPTER = {
    "id": "sra-1",
    "name": "Made-up SRA",
    "vendor": "Made-up Storage",
    "version": "1.0",
    "image_id": "sha256:made-up-image",
    "containers": [],
}


def _mock_sra(mock: respx.MockRouter, *, after: list[Any] | None = None) -> dict[str, respx.Route]:
    _login(mock)
    mock.get(f"{_API}/appliance").respond(
        200, json={"hostname": _APPLIANCE_FQDN, "appliance_version": "9.1.1.0"}
    )
    lists = [[], after if after is not None else [_ADAPTER]]
    replies = [httpx.Response(202, json={"id": f"list-{i}", "status": "RUNNING"}) for i in range(2)]
    tasks = [
        httpx.Response(200, json={"id": f"list-{i}", "status": "SUCCESS", "result": rows})
        for i, rows in enumerate(lists)
    ]
    return {
        "list": mock.get(f"{_API}/storage-replication-adapters").mock(side_effect=replies),
        "task0": mock.get(f"{_API}/tasks/list-0").mock(return_value=tasks[0]),
        "task1": mock.get(f"{_API}/tasks/list-1").mock(return_value=tasks[1]),
        "upload": mock.post(f"{_API}/storage-replication-adapters").respond(200),
        "archive": mock.get(f"https://{_ARCHIVE_HOST}{_ARCHIVE_PATH}").respond(
            200, content=_ARCHIVE
        ),
        "session": mock.get(f"{_API}/session").respond(200, json={}),
    }


def _sra_params(**overrides: Any) -> dict[str, Any]:
    params = {
        "appliance_secret_ref": _ADMIN_REF,
        "archive_url": _ARCHIVE_URL,
        "sha256": _ARCHIVE_SHA,
    }
    params.update(overrides)
    return params


def _no_temp_folders(root: Path) -> None:
    assert not list(root.glob(f"{sra.TEMP_PREFIX}*"))


async def test_install_downloads_checks_uploads_and_lists(
    _stub_embedding: AsyncMock, tmp_path: Path
) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert body["outcome"] == "installed", body
    assert body["installed"] is True
    assert body["adapter"]["name"] == "Made-up SRA"
    assert body["archive"] == {
        "name": "made-up-adapter.tar.gz",
        "source": f"https://{_ARCHIVE_HOST}{_ARCHIVE_PATH}",
        "size": len(_ARCHIVE),
        "sha256": _ARCHIVE_SHA,
        "expected_sha256": _ARCHIVE_SHA,
    }
    upload = routes["upload"].calls[0].request
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert upload.headers["x-dr-session"] == "setup-1"
    assert _ARCHIVE in upload.read()
    assert "authorization" not in routes["archive"].calls[0].request.headers
    assert "x-dr-session" not in routes["archive"].calls[0].request.headers
    assert [row["status"] for row in body["steps"]] == ["done"] * 5
    assert body["cleanup"]["temporary_folder_removed"] is True
    _no_temp_folders(tmp_path)
    _no_secret(body)


async def test_wrong_sha256_uploads_nothing(_stub_embedding: AsyncMock, tmp_path: Path) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        body = await connector.storage_adapter_install(
            _operator(), _Target(), _sra_params(sha256="0" * 64)
        )
    assert body["outcome"] == "failed"
    assert body["installed"] is False
    assert routes["upload"].call_count == 0
    failed = next(row for row in body["steps"] if row["status"] == "failed")
    assert failed["step"] == "check_sha256"
    assert "nothing was uploaded" in failed["error"]
    _no_temp_folders(tmp_path)


async def test_archive_url_that_does_not_answer_installs_nothing(
    _stub_embedding: AsyncMock, tmp_path: Path
) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        routes["archive"].mock(side_effect=httpx.ConnectError("refused"))
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert body["installed"] is False
    assert routes["upload"].call_count == 0
    failed = next(row for row in body["steps"] if row["status"] == "failed")
    assert failed["step"] == "download"
    assert "ConnectError" in failed["error"]
    _no_temp_folders(tmp_path)
    _no_secret(body)


async def test_redirect_to_another_host_is_not_followed(
    _stub_embedding: AsyncMock, tmp_path: Path
) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        routes["archive"].respond(302, headers={"location": "https://elsewhere.test.invalid/x"})
        other = mock.get("https://elsewhere.test.invalid/x").respond(200, content=_ARCHIVE)
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert other.call_count == 0
    failed = next(row for row in body["steps"] if row["status"] == "failed")
    assert "redirect to another host" in failed["error"]
    assert routes["upload"].call_count == 0
    _no_temp_folders(tmp_path)


async def test_broken_upload_says_nothing_was_installed(
    _stub_embedding: AsyncMock, tmp_path: Path
) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock, after=[])
        routes["upload"].mock(side_effect=httpx.ReadError("connection reset"))
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert body["outcome"] == "failed"
    assert body["installed"] is False
    status = {row["step"]: row["status"] for row in body["steps"]}
    assert status == {
        "list_before": "done",
        "download": "done",
        "check_sha256": "done",
        "upload": "failed",
        "list_after": "not_run",
    }
    assert routes["task1"].call_count == 1  # the list was read again after the break
    _no_temp_folders(tmp_path)


async def test_session_is_kept_alive_during_a_slow_download(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = await _connector(_stub_embedding)
    monkeypatch.setattr(sra, "KEEP_ALIVE_SECONDS", 0.01)

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=_ARCHIVE)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        routes["archive"].mock(side_effect=_slow)
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert body["outcome"] == "installed", body
    assert routes["session"].call_count >= 1
    assert routes["session"].calls[0].request.headers["x-dr-session"] == "setup-1"


async def test_keep_alive_error_does_not_lose_the_answer_or_the_folder(
    _stub_embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connector = await _connector(_stub_embedding)
    monkeypatch.setattr(sra, "KEEP_ALIVE_SECONDS", 0.01)

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=_ARCHIVE)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        routes["archive"].mock(side_effect=_slow)
        routes["session"].mock(side_effect=RuntimeError("made-up keep-alive failure"))
        body = await connector.storage_adapter_install(_operator(), _Target(), _sra_params())
    assert routes["session"].call_count >= 1
    assert body["outcome"] == "installed", body
    assert body["cleanup"]["temporary_folder_removed"] is True
    _no_temp_folders(tmp_path)


async def test_old_leftovers_are_removed_and_new_ones_kept(tmp_path: Path) -> None:
    old = tmp_path / f"{sra.TEMP_PREFIX}old"
    new = tmp_path / f"{sra.TEMP_PREFIX}new"
    old.mkdir()
    new.mkdir()
    stale = time.time() - sra.LEFTOVER_AGE_SECONDS - 60
    os.utime(old, (stale, stale))
    assert sra.remove_leftovers() == [old.name]
    assert new.exists()


def _preview_ctx(connector: SrmConnector, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id=sra.OP_ID),
        connector_instance=connector,
        operator=_operator(),
        target=_Target(),
        params=params,
        connector_id=SRM_CONNECTOR_ID,
    )


async def test_preview_names_the_appliance_the_adapters_and_what_cannot_be_undone(
    _stub_embedding: AsyncMock,
) -> None:
    connector = await _connector(_stub_embedding)
    assert _PREVIEW_BUILDERS[sra.OP_ID] is sra.storage_adapter_install_preview
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        routes["list"].mock(
            return_value=httpx.Response(202, json={"id": "list-1", "status": "RUNNING"})
        )
        mock.head(f"https://{_ARCHIVE_HOST}{_ARCHIVE_PATH}").respond(
            200, headers={"content-length": str(len(_ARCHIVE))}
        )
        preview = await sra.storage_adapter_install_preview(_preview_ctx(connector, _sra_params()))
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"]["kind"] == "srm_appliance"
    assert blast["object"]["hostname"] == _APPLIANCE_FQDN
    assert blast["object"]["archive"]["size"] == len(_ARCHIVE)
    assert blast["children"][0]["name"] == "Made-up SRA"
    assert "container" in blast["irreversibility"]
    assert routes["upload"].call_count == 0
    _no_secret(preview)


async def test_preview_refuses_a_missing_archive(_stub_embedding: AsyncMock) -> None:
    connector = await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_sra(mock)
        mock.head(f"https://{_ARCHIVE_HOST}{_ARCHIVE_PATH}").respond(404)
        with pytest.raises(ValueError, match="404"):
            await sra.storage_adapter_install_preview(_preview_ctx(connector, _sra_params()))


async def test_install_is_refused_without_a_matching_preview_hash(
    _stub_embedding: AsyncMock,
) -> None:
    await _connector(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        routes = _mock_sra(mock)
        result = await dispatch(
            operator=_operator(),
            connector_id=SRM_CONNECTOR_ID,
            op_id=sra.OP_ID,
            target=_Target(),
            params=_sra_params(),
        )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert routes["archive"].call_count == 0
    assert routes["upload"].call_count == 0
