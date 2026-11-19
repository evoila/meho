# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the first-start checks (``GET /api/v1/first-start``, ``GET /first-start``).

Covers the four fault injections (wrong host name, expired certificate, a
clock five minutes off, an unreachable target), a name that resolves to the
wrong server, the clean list, the answer without a login (a fixed line, no
check, no system contacted), the tenant scope of the systems, and that no
secret or exception text reaches the answer. The certificate tests do a real
TLS handshake against a local server.
"""

import asyncio
import os
import ssl
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane import first_start
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.main import app
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import require_ui_operator
from meho_backplane.ui.routes import first_start as first_start_page
from meho_backplane.ui.routes.first_start import build_first_start_router

_SITE = "https://meho.example.com"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _SITE)
    monkeypatch.setenv("PARAMS_HASH_KEY", "made-up-test-key-of-this-install")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    app.dependency_overrides.pop(verify_jwt_and_bind, None)


_TENANT = uuid.UUID("00000000-0000-4000-8000-000000000001")
_OTHER_TENANT = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _operator(
    *,
    tenant_id: uuid.UUID = _TENANT,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    platform_admin: bool = False,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub="admin-sub",
        raw_jwt="not-a-real-token",
        tenant_id=tenant_id,
        tenant_role=role,
        platform_admin=platform_admin,
        principal_kind=kind,
    )


def _target(name: str, secret_ref: str | None = None, tenant_id: uuid.UUID = _TENANT) -> Any:
    return SimpleNamespace(name=name, secret_ref=secret_ref, tenant_id=tenant_id)


def _patch_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every outside source answer like a healthy site."""

    async def resolve(host: str, port: int) -> list[str]:
        return ["192.0.2.10"]

    async def cert(host: str, port: int) -> dict[str, object]:
        end = datetime.now(UTC) + timedelta(days=90)
        return {"notAfter": end.strftime("%b %d %H:%M:%S %Y GMT")}

    async def now() -> datetime:
        return datetime.now(UTC)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [_target("esx-1"), _target("esx-2")]

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        return "up"

    async def proof(backplane_url: str, nonce: str) -> str | None:
        return first_start._destination_proof(nonce)

    monkeypatch.setattr(first_start, "_resolve_host", resolve)
    monkeypatch.setattr(first_start, "_fetch_destination_proof", proof)
    monkeypatch.setattr(first_start, "_fetch_peer_certificate", cert)
    monkeypatch.setattr(first_start, "_keycloak_time", now)
    monkeypatch.setattr(first_start, "_database_time", now)
    monkeypatch.setattr(first_start, "_live_targets", targets)
    monkeypatch.setattr(first_start, "_probe_one", probe)


def _get(operator: Operator | None = None) -> dict[str, Any]:
    """The list as a signed-in person sees it (a tenant admin by default)."""
    signed_in = operator or _operator()
    app.dependency_overrides[verify_jwt_and_bind] = lambda: signed_in
    response = TestClient(app).get("/api/v1/first-start")
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


def _red(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in body["checks"] if c["status"] == "fail"]


async def _never_called(*args: Any) -> Any:
    raise AssertionError("must not be called")


def test_clean_site_is_all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    body = _get()
    assert body["ok"] is True
    assert [c["name"] for c in body["checks"]] == [
        "dns",
        "certificate",
        "network",
        "clock",
        "reachability",
    ]
    assert [c["status"] for c in body["checks"]] == ["ok"] * 5
    assert "points at this MEHO" in body["checks"][0]["message"]


def _network_line(body: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in body["checks"] if c["name"] == "network")


def test_public_address_gives_a_warning_not_a_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def public(host: str, port: int) -> list[str]:
        return ["198.51.100.7", "2001:4860:4860::8888"]

    monkeypatch.setattr(first_start, "_resolve_host", public)
    body = _get()
    line = _network_line(body)
    assert line["status"] == "warn"
    assert "never on the internet" in line["message"]
    assert _red(body) == []
    assert body["ok"] is True


def test_global_example_address_gives_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def public(host: str, port: int) -> list[str]:
        return ["8.8.8.8"]

    monkeypatch.setattr(first_start, "_resolve_host", public)
    assert _network_line(_get())["status"] == "warn"


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.5",
        "192.168.1.1",
        "100.64.0.9",
        "127.0.0.1",
        "169.254.1.1",
        "192.0.2.10",
        "fd00::1",
        "fe80::1%eth0",
    ],
)
def test_private_shared_and_local_addresses_give_no_warning(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _patch_green(monkeypatch)

    async def private(host: str, port: int) -> list[str]:
        return [address]

    monkeypatch.setattr(first_start, "_resolve_host", private)
    assert _network_line(_get())["status"] == "ok"


def test_one_public_address_among_private_ones_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def mixed(host: str, port: int) -> list[str]:
        return ["10.0.0.4", "8.8.4.4"]

    monkeypatch.setattr(first_start, "_resolve_host", mixed)
    assert _network_line(_get())["status"] == "warn"


def test_network_line_is_skipped_when_the_name_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_green(monkeypatch)

    async def nothing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(first_start, "_resolve_host", nothing)
    body = _get()
    assert _network_line(body)["status"] == "skipped"
    assert [c["name"] for c in _red(body)] == ["dns"]


def test_wrong_host_name_gives_one_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def nothing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(first_start, "_resolve_host", nothing)
    monkeypatch.setattr(first_start, "_fetch_destination_proof", _never_called)
    body = _get()
    red = _red(body)
    assert body["ok"] is False
    assert [c["name"] for c in red] == ["dns"]
    assert "meho.example.com does not resolve" in red[0]["message"]
    assert "BACKPLANE_URL" in red[0]["message"]
    # The certificate check waits for the DNS fix instead of adding a second red line.
    assert body["checks"][1]["status"] == "skipped"


def test_empty_backplane_url_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    monkeypatch.setenv("BACKPLANE_URL", "")
    get_settings.cache_clear()
    red = _red(_get())
    assert [c["name"] for c in red] == ["dns"]
    assert "BACKPLANE_URL is not set" in red[0]["message"]


def test_clock_five_minutes_off_gives_one_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def late() -> datetime:
        return datetime.now(UTC) + timedelta(minutes=5)

    monkeypatch.setattr(first_start, "_keycloak_time", late)
    red = _red(_get())
    assert [c["name"] for c in red] == ["clock"]
    assert "Keycloak" in red[0]["message"]
    assert "PostgreSQL" not in red[0]["message"]
    assert "NTP" in red[0]["message"]


def test_clock_source_that_cannot_be_read_is_red_not_green(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.db import engine

    _patch_green(monkeypatch)

    def no_database() -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(first_start, "_database_time", _REAL_DATABASE_TIME)
    monkeypatch.setattr(engine, "get_sessionmaker", no_database)
    red = _red(_get())
    assert [c["name"] for c in red] == ["clock"]
    assert "could not read the time from PostgreSQL" in red[0]["message"]


def test_unreachable_target_gives_one_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        return "up" if target.name != "esx-2" else "down"

    monkeypatch.setattr(first_start, "_probe_one", probe)
    red = _red(_get())
    assert [c["name"] for c in red] == ["reachability"]
    assert "These systems did not answer: esx-2." in red[0]["message"]
    assert "esx-1" not in red[0]["message"]


def test_answer_runs_fresh_after_a_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    up = {"esx-2": False}

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        return "up" if up.get(target.name, True) else "down"

    monkeypatch.setattr(first_start, "_probe_one", probe)
    assert [c["name"] for c in _red(_get())] == ["reachability"]
    up["esx-2"] = True  # the operator fixes the system
    assert _get()["ok"] is True


async def test_admin_sees_only_the_systems_of_their_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_green(monkeypatch)
    systems = {
        _TENANT: [_target("esx-1"), _target("esx-2")],
        _OTHER_TENANT: [_target("other-1", tenant_id=_OTHER_TENANT)],
    }
    seen: list[Any] = []
    probed: list[str] = []

    async def targets(tenant_id: Any = None) -> list[Any]:
        seen.append(tenant_id)
        if tenant_id is None:
            return [t for group in systems.values() for t in group]
        return systems[tenant_id]

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        probed.append(target.name)
        return "down"

    monkeypatch.setattr(first_start, "_live_targets", targets)
    monkeypatch.setattr(first_start, "_probe_one", probe)
    line = _get(_operator())["checks"][4]
    assert seen == [_TENANT]
    assert sorted(probed) == ["esx-1", "esx-2"]
    assert "These systems did not answer: esx-1, esx-2." in line["message"]
    assert "other-1" not in line["message"]


@pytest.mark.parametrize(
    ("operator", "scope"),
    [
        (_operator(), _TENANT),
        (_operator(role=TenantRole.OPERATOR), _TENANT),
        (_operator(platform_admin=True), None),
        (_operator(platform_admin=True, kind=PrincipalKind.AGENT), _TENANT),
    ],
    ids=["tenant-admin", "operator", "platform-admin", "platform-admin-machine-login"],
)
def test_platform_admin_person_sees_all_tenants(operator: Operator, scope: Any) -> None:
    assert first_start.reachability_scope(operator) == scope


def test_platform_admin_list_covers_all_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    seen: list[Any] = []

    async def targets(tenant_id: Any = None) -> list[Any]:
        seen.append(tenant_id)
        return [_target("esx-1"), _target("other-1", tenant_id=_OTHER_TENANT)]

    monkeypatch.setattr(first_start, "_live_targets", targets)
    assert _get(_operator(platform_admin=True))["checks"][4]["status"] == "ok"
    assert seen == [None]


def test_no_targets_is_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def none(tenant_id: Any = None) -> list[Any]:
        return []

    monkeypatch.setattr(first_start, "_live_targets", none)
    assert _get()["checks"][3]["status"] == "ok"


def test_unreadable_target_list_is_red_not_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def broken(tenant_id: Any = None) -> list[Any]:
        raise RuntimeError("hunter2 db down")

    monkeypatch.setattr(first_start, "_live_targets", broken)
    body = _get()
    assert [c["name"] for c in _red(body)] == ["reachability"]
    assert "hunter2" not in str(body)


def test_answer_holds_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def raising(host: str, port: int) -> dict[str, object]:
        raise ssl.SSLError("Traceback s3cret-value")

    monkeypatch.setattr(first_start, "_fetch_peer_certificate", raising)
    body = _get()
    text = str(body)
    assert "s3cret-value" not in text
    assert "Traceback" not in text
    assert "not-a-real-token" not in text
    assert set(body) == {"ok", "checks"}


def test_without_login_the_answer_is_one_fixed_line_and_no_system_is_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connector(monkeypatch)
    calls: list[str] = []

    async def targets(tenant_id: Any = None) -> list[Any]:
        calls.append("targets")
        return [_target("esx-1"), _target("esx-2", tenant_id=_OTHER_TENANT)]

    async def count(*args: Any) -> Any:
        calls.append("site")
        raise AssertionError("no check runs without a login")

    monkeypatch.setattr(first_start, "_live_targets", targets)
    for name in ("_resolve_host", "_fetch_peer_certificate", "_keycloak_time", "_database_time"):
        monkeypatch.setattr(first_start, name, count)
    for _ in range(3):
        response = TestClient(app).get("/first-start")  # no Authorization header
        assert response.status_code == 200
        assert response.json() == first_start.SIGN_IN_ANSWER.model_dump()
    assert calls == []
    assert _Fingerprint.seen == []  # the fake target was never contacted
    text = response.text
    assert "system" not in text
    assert "meho.example.com" not in text


def test_signed_in_list_needs_a_login_and_the_operator_role() -> None:
    assert TestClient(app).get("/api/v1/first-start").status_code == 401
    reader = _operator(role=TenantRole.READ_ONLY)
    app.dependency_overrides[verify_jwt_and_bind] = lambda: reader
    response = TestClient(app).get("/api/v1/first-start")
    assert response.status_code == 403
    assert "checks" not in response.text


# --- real TLS handshakes ----------------------------------------------------


def _make_cert(
    *, dns_name: str | None, ip: str | None, not_before: datetime, not_after: datetime
) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "meho-test")])
    sans: list[x509.GeneralName] = []
    if dns_name:
        sans.append(x509.DNSName(dns_name))
    if ip:
        sans.append(x509.IPAddress(ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


@asynccontextmanager
async def _tls_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dns_name: str | None = None,
    ip: str | None = "127.0.0.1",
    days_left: int = 90,
) -> AsyncIterator[int]:
    now = datetime.now(UTC)
    cert_pem, key_pem = _make_cert(
        dns_name=dns_name,
        ip=ip,
        not_before=now - timedelta(days=400),
        not_after=now + timedelta(days=days_left),
    )
    with tempfile.TemporaryDirectory() as folder:
        cert_file, key_file = Path(folder, "c.pem"), Path(folder, "k.pem")
        cert_file.write_bytes(cert_pem)
        key_file.write_bytes(key_pem)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_file, key_file)

        def trust() -> ssl.SSLContext:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cadata=cert_pem.decode())
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            return ctx

        monkeypatch.setattr(first_start, "_trust_context", trust)

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()


def _with_server(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> first_start.FirstStartLine:
    async def run() -> first_start.FirstStartLine:
        async with _tls_server(monkeypatch, **kwargs) as port:
            return await first_start.check_certificate(f"https://127.0.0.1:{port}", dns_ok=True)

    return asyncio.run(run())


def test_valid_certificate_is_green(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _with_server(monkeypatch, days_left=90)
    assert line.status == "ok", line.message


def test_expired_certificate_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _with_server(monkeypatch, days_left=-1)
    assert line.status == "fail"
    assert "has expired" in line.message


def test_certificate_with_less_than_30_days_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _with_server(monkeypatch, days_left=10)
    assert line.status == "fail"
    assert "ends in" in line.message
    assert "more than 30 days" in line.message


def test_certificate_for_another_name_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    line = _with_server(monkeypatch, dns_name="other.example.com", ip=None)
    assert line.status == "fail"
    assert "does not match the host name" in line.message


def test_untrusted_certificate_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> first_start.FirstStartLine:
        async with _tls_server(monkeypatch) as port:
            monkeypatch.setattr(first_start, "_trust_context", ssl.create_default_context)
            return await first_start.check_certificate(f"https://127.0.0.1:{port}", dns_ok=True)

    line = asyncio.run(run())
    assert line.status == "fail"
    assert "not trusted" in line.message


def test_closed_port_is_red() -> None:
    line = asyncio.run(first_start.check_certificate("https://127.0.0.1:9", dns_ok=True))
    assert line.status == "fail"
    assert "could not connect" in line.message


def test_plain_http_skips_the_certificate_check() -> None:
    line = asyncio.run(first_start.check_certificate("http://meho.example.com", dns_ok=True))
    assert line.status == "skipped"


def test_url_edge_inputs() -> None:
    assert first_start._site_host_port("") is None
    assert first_start._site_host_port("not a url") is None
    assert first_start._site_host_port("ftp://x") is None
    assert first_start._site_host_port("https://[::1]:8443/a") == ("::1", 8443, "https")


def test_console_page_shows_the_same_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def late() -> datetime:
        return datetime.now(UTC) + timedelta(minutes=5)

    monkeypatch.setattr(first_start, "_database_time", late)
    person = _operator()
    operators_seen: list[Any] = []

    async def lift(session_ctx: Any) -> Any:
        return person

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        operators_seen.append(operator)
        return "up"

    monkeypatch.setattr(first_start_page, "resolve_ui_operator", lift)
    monkeypatch.setattr(first_start, "_probe_one", probe)
    page_app = FastAPI()
    page_app.include_router(build_first_start_router())
    page_app.dependency_overrides[require_ui_operator] = lambda: SimpleNamespace(tenant_id=_TENANT)
    page = TestClient(page_app).get("/ui/first-start")
    assert page.status_code == 200
    # The console and the API pass the signed-in person to every probe.
    assert operators_seen == [person, person]
    operators_seen.clear()
    api = _get(person)
    assert operators_seen == [person, person]
    for line in api["checks"]:
        marker = f'data-check="{line["name"]}" data-status="{line["status"]}"'
        assert marker in page.text
        assert line["message"] in page.text


def test_console_page_checks_the_session_tenant_when_the_role_lift_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_green(monkeypatch)
    seen: list[Any] = []

    async def no_lift(session_ctx: Any) -> Any:
        return None

    async def targets(tenant_id: Any = None) -> list[Any]:
        seen.append(tenant_id)
        return []

    monkeypatch.setattr(first_start_page, "resolve_ui_operator", no_lift)
    monkeypatch.setattr(first_start, "_live_targets", targets)
    page_app = FastAPI()
    page_app.include_router(build_first_start_router())
    page_app.dependency_overrides[require_ui_operator] = lambda: SimpleNamespace(
        tenant_id=_OTHER_TENANT
    )
    assert TestClient(page_app).get("/ui/first-start").status_code == 200
    assert seen == [_OTHER_TENANT]


# --- the destination of the DNS name ----------------------------------------


def test_name_that_resolves_to_the_wrong_server_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def not_meho(backplane_url: str, nonce: str) -> str | None:
        return None  # the server answers 404, or not at all

    monkeypatch.setattr(first_start, "_fetch_destination_proof", not_meho)
    body = _get()
    red = _red(body)
    assert [c["name"] for c in red] == ["dns"]
    assert "does not answer as this MEHO" in red[0]["message"]
    assert "BACKPLANE_URL" in red[0]["message"]
    assert body["checks"][1]["status"] == "ok"  # the certificate of the wrong server is fine


def test_name_that_resolves_to_another_meho_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def other_install(backplane_url: str, nonce: str) -> str | None:
        return first_start._destination_proof("0" * 32)  # a proof for another nonce

    monkeypatch.setattr(first_start, "_fetch_destination_proof", other_install)
    assert [c["name"] for c in _red(_get())] == ["dns"]


def test_destination_waits_for_the_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def bad_certificate(host: str, port: int) -> dict[str, object]:
        raise ssl.SSLError("certificate verify failed")

    monkeypatch.setattr(first_start, "_fetch_peer_certificate", bad_certificate)
    monkeypatch.setattr(first_start, "_fetch_destination_proof", _never_called)
    body = _get()
    assert [c["name"] for c in _red(body)] == ["certificate"]
    assert body["checks"][0]["status"] == "ok"
    assert "once the certificate check is green" in body["checks"][0]["message"]


def test_destination_fetch_reads_the_proof_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        nonce = request.url.params["nonce"]
        return httpx.Response(200, json={"proof": first_start._destination_proof(nonce)})

    async def run() -> tuple[str | None, str | None, str | None]:
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://meho.example.com/first-start/destination").mock(side_effect=answer)
            mock.get("https://wrong.example.com/first-start/destination").mock(
                return_value=httpx.Response(404, text="<html>not found</html>")
            )
            mock.get("https://down.example.com/first-start/destination").mock(
                side_effect=httpx.ConnectError("refused")
            )
            nonce = "ab" * 16
            return (
                await first_start._fetch_destination_proof(_SITE + "/", nonce),
                await first_start._fetch_destination_proof("https://wrong.example.com", nonce),
                await first_start._fetch_destination_proof("https://down.example.com", nonce),
            )

    right, wrong, down = asyncio.run(run())
    assert right == first_start._destination_proof("ab" * 16)
    assert wrong is None
    assert down is None


def test_destination_endpoint_answers_the_proof_without_login() -> None:
    nonce = "ab" * 16
    response = TestClient(app).get("/first-start/destination", params={"nonce": nonce})
    assert response.status_code == 200
    assert response.json() == {"proof": first_start._destination_proof(nonce)}
    assert (
        TestClient(app).get("/first-start/destination", params={"nonce": "short"}).status_code
        == 422
    )


# --- the clock source must answer with success ------------------------------


_REAL_KEYCLOAK_TIME = first_start._keycloak_time
_REAL_DATABASE_TIME = first_start._database_time
_DISCOVERY = "https://keycloak.test/realms/meho/.well-known/openid-configuration"


def _date_now() -> dict[str, str]:
    return {"date": format_datetime(datetime.now(UTC), usegmt=True)}


async def _read_keycloak(
    answer: httpx.Response | Exception, url: str = _DISCOVERY
) -> first_start.ClockReading:
    """The real Keycloak reader against one discovery answer, or a failed request."""
    with respx.mock() as mock:
        route = mock.get(url)
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        return await _REAL_KEYCLOAK_TIME()


async def _keycloak_time_with(status: int) -> first_start.ClockReading:
    """The real Keycloak reader against a discovery answer with the current date."""
    return await _read_keycloak(httpx.Response(status, headers=_date_now()))


def _clock_line(body: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in body["checks"] if c["name"] == "clock")


async def test_keycloak_time_comes_from_a_successful_answer_only() -> None:
    reading = await _keycloak_time_with(200)
    assert isinstance(reading, datetime)
    assert abs((datetime.now(UTC) - reading).total_seconds()) < 5
    # A 503 from a proxy carries the proxy's time, not Keycloak's.
    error_page = await _keycloak_time_with(503)
    assert isinstance(error_page, first_start.NoClockReading)
    assert error_page.red is True


def test_clock_is_red_when_keycloak_answers_an_error_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def error_page() -> first_start.ClockReading:
        return await _keycloak_time_with(503)

    monkeypatch.setattr(first_start, "_keycloak_time", error_page)
    red = _red(_get())
    assert [c["name"] for c in red] == ["clock"]
    assert "Keycloak answered with HTTP 503" in red[0]["message"]
    assert "KEYCLOAK_ISSUER_URL" in red[0]["message"]


# --- Keycloak without a Date header -----------------------------------------


@pytest.mark.parametrize("headers", [{}, {"date": ""}, {"date": "not a date"}])
async def test_keycloak_answer_without_a_usable_date_is_not_compared(
    headers: dict[str, str],
) -> None:
    reading = await _read_keycloak(httpx.Response(200, headers=headers, json={"issuer": "x"}))
    assert isinstance(reading, first_start.NoClockReading)
    assert reading.red is False
    assert "no Date header" in reading.message


def test_clock_is_green_when_keycloak_sends_no_date_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The core's Keycloak answers its discovery address with 200 and no Date header.
    _patch_green(monkeypatch)

    async def no_date() -> first_start.ClockReading:
        return await _read_keycloak(httpx.Response(200, json={"issuer": "x"}))

    monkeypatch.setattr(first_start, "_keycloak_time", no_date)
    body = _get()
    clock = _clock_line(body)
    assert body["ok"] is True
    assert clock["status"] == "ok"
    assert "agrees with PostgreSQL within 60 seconds" in clock["message"]
    assert "Keycloak was not compared" in clock["message"]
    assert "no Date header" in clock["message"]
    assert "reach" not in clock["message"]


async def test_clock_off_from_postgresql_is_red_and_still_names_the_missing_date() -> None:
    async def no_date() -> first_start.ClockReading:
        return first_start.NoClockReading(message="Keycloak has no Date header.", red=False)

    async def late() -> first_start.ClockReading:
        return datetime.now(UTC) + timedelta(minutes=5)

    line = await first_start.check_clock(keycloak_time=no_date, database_time=late)
    assert line.status == "fail"
    assert "away from the clock of PostgreSQL" in line.message
    assert "Keycloak has no Date header." in line.message


async def test_clock_line_is_skipped_when_no_source_is_compared() -> None:
    async def left_out() -> first_start.ClockReading:
        return first_start.NoClockReading(message="Not compared.", red=False)

    line = await first_start.check_clock(keycloak_time=left_out, database_time=left_out)
    assert line.status == "skipped"
    assert line.message == "Not compared. Not compared."


# --- Keycloak that cannot be read names the real cause ----------------------


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow")],
)
async def test_keycloak_without_an_answer_names_the_connection(error: Exception) -> None:
    reading = await _read_keycloak(error)
    assert isinstance(reading, first_start.NoClockReading)
    assert reading.red is True
    assert "got no answer from Keycloak" in reading.message
    assert "KEYCLOAK_ISSUER_URL" in reading.message
    assert "Date" not in reading.message


async def test_keycloak_answer_that_cannot_be_read_is_red() -> None:
    reading = await _read_keycloak(httpx.DecodingError("bad gzip"))
    assert isinstance(reading, first_start.NoClockReading)
    assert reading.red is True
    assert "could not read the answer of Keycloak" in reading.message


async def test_keycloak_fix_names_the_discovery_setting_when_it_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "http://keycloak-in-cluster.test:8080/auth/realms/meho"
    monkeypatch.setenv("KEYCLOAK_DISCOVERY_URL", base)
    get_settings.cache_clear()
    reading = await _read_keycloak(
        httpx.ConnectError("refused"), url=f"{base}/.well-known/openid-configuration"
    )
    assert isinstance(reading, first_start.NoClockReading)
    assert "KEYCLOAK_DISCOVERY_URL" in reading.message
    assert "keycloak-in-cluster.test" not in reading.message


@pytest.fixture
def _local_time_not_utc() -> Iterator[None]:
    """The local time of this process is five hours behind UTC (a POSIX zone, no tzdata)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "EST5"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.mark.usefixtures("_local_time_not_utc")
def test_http_date_reads_every_zone_as_utc() -> None:
    expected = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    assert first_start._http_date("Mon, 16 Nov 2026 10:00:00 GMT") == expected
    # A -0000 zone parses to a naive value that means UTC, not the local time.
    assert first_start._http_date("Mon, 16 Nov 2026 10:00:00 -0000") == expected
    assert first_start._http_date("Mon, 16 Nov 2026 12:00:00 +0200") == expected
    assert first_start._http_date(None) is None


# --- systems whose login is in Vault ----------------------------------------


class _Fingerprint:
    """A fake connector: records the operator and answers "reachable"."""

    seen: ClassVar[list[Any]] = []

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        self.seen.append(operator)
        return SimpleNamespace(reachable=True)


def _use_fake_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.connectors import resolver

    _Fingerprint.seen.clear()
    monkeypatch.setattr(
        resolver, "resolve_connector_or_label", lambda target, **_kw: (_Fingerprint, None, None)
    )


_VAULT_NOTE = "login is in Vault and MEHO reads it only for a person signed in to their tenant"


async def test_signed_in_person_is_passed_to_a_system_with_login_in_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connector(monkeypatch)
    person = _operator()

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [_target("k8s-1", secret_ref="secret/k8s-1"), _target("esx-1")]

    line = await first_start.check_reachability(operator=person, list_targets=targets)
    assert line.status == "ok", line.message
    assert _Fingerprint.seen == [person, person]


async def test_system_with_login_in_vault_is_not_contacted_without_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connector(monkeypatch)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [_target("k8s-1", secret_ref="secret/k8s-1"), _target("esx-1")]

    line = await first_start.check_reachability(operator=None, list_targets=targets)
    assert line.status == "skipped"
    assert "1 of 2 systems answered" in line.message
    assert "These systems could not be checked, because their login is in Vault" in line.message
    assert "k8s-1" in line.message
    assert _Fingerprint.seen == [None]  # only esx-1 was contacted


async def test_login_in_vault_of_another_tenant_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connector(monkeypatch)
    platform_admin = _operator(platform_admin=True)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [
            _target("k8s-1", secret_ref="secret/k8s-1"),
            _target("k8s-9", secret_ref="secret/k8s-9", tenant_id=_OTHER_TENANT),
            _target("esx-9", tenant_id=_OTHER_TENANT),
        ]

    line = await first_start.check_reachability(
        operator=platform_admin, tenant_id=None, list_targets=targets
    )
    assert line.status == "skipped"
    assert "2 of 3 systems answered" in line.message
    assert f"{_VAULT_NOTE}: k8s-9." in line.message
    assert _Fingerprint.seen == [platform_admin, platform_admin]  # k8s-1 and esx-9


@pytest.mark.parametrize("kind", [PrincipalKind.AGENT, PrincipalKind.SERVICE])
async def test_machine_login_leaves_systems_with_login_in_vault_unchecked(
    monkeypatch: pytest.MonkeyPatch, kind: PrincipalKind
) -> None:
    _use_fake_connector(monkeypatch)
    machine = _operator(kind=kind, platform_admin=True)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [
            _target("k8s-1", secret_ref="secret/k8s-1"),
            _target("k8s-2", secret_ref="vault:secret/k8s-2"),
            _target("esx-1"),
        ]

    line = await first_start.check_reachability(operator=machine, list_targets=targets)
    assert line.status == "skipped"
    assert "1 of 3 systems answered" in line.message
    assert f"{_VAULT_NOTE}: k8s-1, k8s-2." in line.message
    assert _Fingerprint.seen == [machine]  # only esx-1 was contacted


async def test_login_in_google_secret_manager_is_checked_in_every_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_connector(monkeypatch)
    platform_admin = _operator(platform_admin=True)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [
            _target("gke-9", secret_ref="gsm:proj-9/gke-9", tenant_id=_OTHER_TENANT),
            _target("k8s-9", secret_ref="secret/k8s-9", tenant_id=_OTHER_TENANT),
        ]

    line = await first_start.check_reachability(
        operator=platform_admin, tenant_id=None, list_targets=targets
    )
    assert line.status == "skipped"
    assert "1 of 2 systems answered" in line.message
    assert f"{_VAULT_NOTE}: k8s-9." in line.message
    assert "gke-9" not in line.message
    assert _Fingerprint.seen == [platform_admin]  # gke-9 only


async def test_ref_without_scheme_follows_the_default_credential_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CREDENTIAL_BACKEND", "gsm")
    get_settings.cache_clear()
    _use_fake_connector(monkeypatch)
    platform_admin = _operator(platform_admin=True)

    async def targets(tenant_id: Any = None) -> list[Any]:
        return [
            _target("gke-9", secret_ref="proj-9/gke-9", tenant_id=_OTHER_TENANT),
            _target("k8s-9", secret_ref="vault:secret/k8s-9", tenant_id=_OTHER_TENANT),
        ]

    line = await first_start.check_reachability(
        operator=platform_admin, tenant_id=None, list_targets=targets
    )
    assert line.status == "skipped"
    assert f"{_VAULT_NOTE}: k8s-9." in line.message
    assert _Fingerprint.seen == [platform_admin]  # gke-9 only


async def test_unchecked_systems_are_told_apart_from_systems_that_did_not_answer() -> None:
    async def targets(tenant_id: Any = None) -> list[Any]:
        return [_target("k8s-1", secret_ref="secret/k8s-1"), _target("esx-1"), _target("esx-2")]

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        if target.secret_ref:
            return "needs_person"
        return "down" if target.name == "esx-2" else "up"

    line = await first_start.check_reachability(list_targets=targets, probe_one=probe)
    assert line.status == "fail"
    assert "These systems did not answer: esx-2." in line.message
    assert f"{_VAULT_NOTE}: k8s-1." in line.message


async def test_many_names_are_cut_short() -> None:
    async def targets(tenant_id: Any = None) -> list[Any]:
        return [_target(f"esx-{n:02d}") for n in range(13)]

    async def probe(target: Any, operator: Any) -> first_start.ProbeOutcome:
        return "down"

    line = await first_start.check_reachability(list_targets=targets, probe_one=probe)
    assert "esx-09 and 3 more." in line.message
    assert "esx-10" not in line.message
