# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the first-start checks (``GET /first-start``).

Covers the four fault injections (wrong host name, expired certificate, a
clock five minutes off, an unreachable target), the clean list, the missing
login, and that no secret or exception text reaches the answer. The
certificate tests do a real TLS handshake against a local server.
"""

import asyncio
import ssl
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane import first_start
from meho_backplane.main import app
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import require_ui_operator
from meho_backplane.ui.routes.first_start import build_first_start_router

_SITE = "https://meho.example.com"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BACKPLANE_URL", _SITE)
    get_settings.cache_clear()
    first_start.reset_public_cache_for_testing()
    yield
    get_settings.cache_clear()
    first_start.reset_public_cache_for_testing()


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
        return [SimpleNamespace(name="esx-1"), SimpleNamespace(name="esx-2")]

    async def probe(target: Any) -> bool:
        return True

    monkeypatch.setattr(first_start, "_resolve_host", resolve)
    monkeypatch.setattr(first_start, "_fetch_peer_certificate", cert)
    monkeypatch.setattr(first_start, "_keycloak_time", now)
    monkeypatch.setattr(first_start, "_database_time", now)
    monkeypatch.setattr(first_start, "_live_targets", targets)
    monkeypatch.setattr(first_start, "_probe_one", probe)


def _get() -> dict[str, Any]:
    response = TestClient(app).get("/first-start")
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


def _red(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in body["checks"] if c["status"] == "fail"]


def test_clean_site_is_all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    body = _get()
    assert body["ok"] is True
    assert [c["name"] for c in body["checks"]] == ["dns", "certificate", "clock", "reachability"]
    assert [c["status"] for c in body["checks"]] == ["ok"] * 4


def test_wrong_host_name_gives_one_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def nothing(host: str, port: int) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(first_start, "_resolve_host", nothing)
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
    _patch_green(monkeypatch)

    async def unreadable() -> None:
        return None

    monkeypatch.setattr(first_start, "_database_time", unreadable)
    red = _red(_get())
    assert [c["name"] for c in red] == ["clock"]
    assert "could not read the time from PostgreSQL" in red[0]["message"]


def test_unreachable_target_gives_one_red_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def probe(target: Any) -> bool:
        return target.name != "esx-2"

    monkeypatch.setattr(first_start, "_probe_one", probe)
    red = _red(_get())
    assert [c["name"] for c in red] == ["reachability"]
    assert "1 of 2 systems did not answer" in red[0]["message"]
    assert "esx-1" not in red[0]["message"]
    assert "esx-2" not in red[0]["message"]


def test_public_answer_is_reused_for_a_short_time(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)
    calls = 0

    async def probe(target: Any) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(first_start, "_probe_one", probe)
    clock = [1000.0]
    monkeypatch.setattr(first_start.time, "monotonic", lambda: clock[0])
    _get()
    _get()
    assert calls == 2  # two systems, one run
    clock[0] += first_start._PUBLIC_CACHE_TTL_S + 1
    _get()
    assert calls == 4


async def test_names_show_only_when_asked_and_only_for_the_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    _patch_green(monkeypatch)
    seen: list[Any] = []
    tenant = uuid.uuid4()

    async def targets(tenant_id: Any = None) -> list[Any]:
        seen.append(tenant_id)
        return [SimpleNamespace(name="esx-1"), SimpleNamespace(name="esx-2")]

    async def probe(target: Any) -> bool:
        return target.name != "esx-2"

    monkeypatch.setattr(first_start, "_live_targets", targets)
    monkeypatch.setattr(first_start, "_probe_one", probe)
    named = await first_start.run_first_start_checks(tenant_id=tenant, show_names=True)
    assert "esx-2" in named.checks[3].message
    assert seen == [tenant]
    plain = await first_start.run_first_start_checks()
    assert "esx-2" not in plain.checks[3].message


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


def test_answer_needs_no_login_and_holds_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_green(monkeypatch)

    async def raising(host: str, port: int) -> dict[str, object]:
        raise ssl.SSLError("Traceback s3cret-value")

    monkeypatch.setattr(first_start, "_fetch_peer_certificate", raising)
    response = TestClient(app).get("/first-start")  # no Authorization header
    assert response.status_code == 200
    text = response.text
    assert "s3cret-value" not in text
    assert "Traceback" not in text
    assert "Bearer" not in text
    assert set(response.json()) == {"ok", "checks"}


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
    page_app = FastAPI()
    page_app.include_router(build_first_start_router())
    page_app.dependency_overrides[require_ui_operator] = lambda: SimpleNamespace(tenant_id=None)
    page = TestClient(page_app).get("/ui/first-start")
    assert page.status_code == 200
    api = _get()
    for line in api["checks"]:
        marker = f'data-check="{line["name"]}" data-status="{line["status"]}"'
        assert marker in page.text
        assert line["message"] in page.text
