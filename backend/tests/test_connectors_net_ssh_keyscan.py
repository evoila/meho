# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.ssh_keyscan`` — SSH host-key pin, ssh-keyscan parity (#3556).

A ``net.*`` sibling probe on the T1 keystone. Covers:

* The op fetches the server's presented host key(s) over the SSH
  handshake **without authenticating** — a real in-process asyncssh
  server presenting an ed25519 **and** an RSA host key is scanned over
  loopback and both are returned, leaf shape complete.
* The returned ``known_hosts`` block round-trips through
  ``asyncssh.import_known_hosts`` and pins the server's actual host key
  (fingerprint identity) — the probe → pin loop #3467's fail-closed
  verification requires.
* ``key_types`` scopes the scan; a server offering none of the requested
  types yields ``scanned=false`` / ``reason="no_host_key"`` (status ok).
* ``host`` is probe-allowlist-gated (T1 foundation); an un-allowlisted
  host fails the dispatch with ``connector_probe_refused`` before any
  socket opens — the ``net.tls_inspect`` refusal shape.
* The return-failures contract: refused / timeout / DNS / unreachable
  return ``scanned=false`` with a reason code and ``status="ok"``.
* The audit row records the literal ``host``/``port``; the op registers
  ``safe`` + ungated; ``net.ssh_keyscan`` classifies as ``read``.

The SSH server runs in-process on the same event loop; the handler dials
it over loopback (added to ``MEHO_NETDIAG_PROBE_ALLOWLIST``).
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import asyncssh
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import ssh_keyscan as net_ssh
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.ssh_keyscan import (
    net_ssh_keyscan,
    register_net_ssh_keyscan_operation,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.ssh_keyscan"


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation (mirrors the sibling probe modules)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_ssh_keyscan_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    await register_net_ssh_keyscan_operation(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_scan(params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# In-process SSH server test double
# ---------------------------------------------------------------------------


class _NoAuthServer(asyncssh.SSHServer):
    """An SSH server that needs no authentication (irrelevant to a keyscan).

    ``get_server_host_key`` disconnects right after the key exchange and
    never authenticates, so this only has to complete the handshake and
    present its host keys.
    """

    def begin_auth(self, username: str) -> bool:
        return False


async def _start_ssh_server(
    tmp_path: Path, host_key_algs: list[str]
) -> tuple[asyncssh.SSHAcceptor, int, dict[str, asyncssh.SSHKey]]:
    """Start a loopback SSH server presenting one host key per *host_key_algs*.

    Returns ``(acceptor, port, {alg: public_key})`` so a test can assert
    fingerprint identity against exactly what the server serves.
    """
    key_paths: list[str] = []
    public_by_alg: dict[str, asyncssh.SSHKey] = {}
    for i, alg in enumerate(host_key_algs):
        key = (
            asyncssh.generate_private_key(alg, key_size=2048)
            if alg == "ssh-rsa"
            else asyncssh.generate_private_key(alg)
        )
        path = tmp_path / f"host_key_{i}"
        key.write_private_key(str(path))
        key_paths.append(str(path))
        public_by_alg[alg] = key.convert_to_public()
    acceptor = await asyncssh.create_server(
        _NoAuthServer, "127.0.0.1", 0, server_host_keys=key_paths
    )
    port = acceptor.sockets[0].getsockname()[1]
    return acceptor, port, public_by_alg


# ---------------------------------------------------------------------------
# Full scan — multiple key types, real handshake, fresh boot
# ---------------------------------------------------------------------------


async def test_scans_multiple_host_key_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    """A server presenting ed25519 + RSA yields both keys, correctly shaped."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    acceptor, port, _publics = await _start_ssh_server(tmp_path, ["ssh-ed25519", "ssh-rsa"])
    try:
        result = await _dispatch_scan({"host": "127.0.0.1", "port": port})
    finally:
        acceptor.close()
        await acceptor.wait_closed()

    assert result.status == "ok", result.error
    body = result.result
    assert body["scanned"] is True
    assert body["reason"] is None
    types = {k["type"] for k in body["keys"]}
    assert types == {"ssh-ed25519", "ssh-rsa"}
    for entry in body["keys"]:
        assert entry["base64"]
        assert entry["sha256_fingerprint"].startswith("SHA256:")
        assert entry["md5_fingerprint"].startswith("MD5:")
        # The known_hosts line is '<host-field> <type> <base64>'; the
        # loopback server is on a non-22 port, so the bracketed form.
        assert entry["known_hosts_line"] == f"[127.0.0.1]:{port} {entry['type']} {entry['base64']}"
    # The joined block contains one line per collected key.
    assert body["known_hosts"].splitlines() == [k["known_hosts_line"] for k in body["keys"]]
    assert "vault kv patch" in body["note"]


async def test_known_hosts_block_pins_the_actual_server_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    """The returned known_hosts round-trips and matches the served host keys.

    This is the probe -> pin loop the fail-closed SshConnector needs: the
    ``known_hosts`` block parses via ``asyncssh.import_known_hosts`` and each
    scanned key's SHA256 fingerprint equals the fingerprint of the key the
    server actually presented.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    acceptor, port, publics = await _start_ssh_server(tmp_path, ["ssh-ed25519", "ssh-rsa"])
    try:
        result = await _dispatch_scan({"host": "127.0.0.1", "port": port})
    finally:
        acceptor.close()
        await acceptor.wait_closed()
    assert result.status == "ok", result.error
    body = result.result

    # Parses as a real known_hosts store (the exact shape SshConnector pins).
    known_hosts = asyncssh.import_known_hosts(body["known_hosts"])
    assert known_hosts is not None

    # Each scanned key's fingerprint equals the served key's fingerprint.
    by_type = {k["type"]: k for k in body["keys"]}
    for alg, public in publics.items():
        assert by_type[alg]["sha256_fingerprint"] == public.get_fingerprint("sha256")
        # base64 is the exact blob the server presented (identity, not shape).
        assert by_type[alg]["base64"] == public.export_public_key("openssh").decode().split()[1]


async def test_key_types_param_scopes_the_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    """``key_types`` restricts which host-key types are fetched."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    acceptor, port, _ = await _start_ssh_server(tmp_path, ["ssh-ed25519", "ssh-rsa"])
    try:
        result = await _dispatch_scan(
            {"host": "127.0.0.1", "port": port, "key_types": ["ssh-ed25519"]}
        )
    finally:
        acceptor.close()
        await acceptor.wait_closed()
    assert result.status == "ok", result.error
    body = result.result
    assert body["scanned"] is True
    assert [k["type"] for k in body["keys"]] == ["ssh-ed25519"]


async def test_no_host_key_when_server_offers_none_of_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    """A server without any requested type ⇒ scanned=false / no_host_key, status ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    # Server presents only ed25519; ask for RSA.
    acceptor, port, _ = await _start_ssh_server(tmp_path, ["ssh-ed25519"])
    try:
        result = await _dispatch_scan({"host": "127.0.0.1", "port": port, "key_types": ["ssh-rsa"]})
    finally:
        acceptor.close()
        await acceptor.wait_closed()
    assert result.status == "ok", result.error
    body = result.result
    assert body["scanned"] is False
    assert body["reason"] == "no_host_key"
    assert body["keys"] == []
    assert body["known_hosts"] == ""


# ---------------------------------------------------------------------------
# Probe allowlist (T1 foundation) + audit row records host:port
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_socket_opens(
    monkeypatch: pytest.MonkeyPatch,
    _registered_ssh_keyscan_op: None,
) -> None:
    """Empty allowlist ⇒ structured refusal, no handshake attempted."""

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("get_server_host_key must not run when the probe is refused")

    monkeypatch.setattr(net_ssh.asyncssh, "get_server_host_key", _boom)
    result = await _dispatch_scan({"host": "10.1.2.3", "port": 22})
    assert result.status == "error"
    assert result.result is None
    assert result.extras["error_code"] == "connector_probe_refused"
    assert result.extras["host"] == "10.1.2.3"
    assert result.error is not None
    assert PROBE_ALLOWLIST_ENV in result.error


async def test_audit_row_records_literal_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    acceptor, port, _ = await _start_ssh_server(tmp_path, ["ssh-ed25519"])
    try:
        result = await _dispatch_scan({"host": "127.0.0.1", "port": port})
    finally:
        acceptor.close()
        await acceptor.wait_closed()
    assert result.status == "ok", result.error

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = list(
            (await session.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
        )
    assert len(rows) == 1
    raw = rows[0].raw_payload
    assert raw is not None
    assert raw["host"] == "127.0.0.1"
    assert raw["port"] == port


# ---------------------------------------------------------------------------
# Return-failures contract — a failed scan is status=ok, never connector_*
# ---------------------------------------------------------------------------


async def test_refused_connect_is_ok_status_not_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_ssh_keyscan_op: None,
) -> None:
    """A closed port returns scanned=false / reason=refused, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # port is now (almost certainly) closed

    result = await _dispatch_scan({"host": "127.0.0.1", "port": port})
    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    assert result.result["scanned"] is False
    assert result.result["reason"] == "refused"
    assert result.result["keys"] == []


@pytest.mark.parametrize(
    "exc,expected_reason",
    [
        (TimeoutError(), "timeout"),
        (socket.gaierror("name resolution failed"), "dns_failure"),
        (ConnectionRefusedError(), "refused"),
        (OSError("network is unreachable"), "unreachable"),
        (asyncssh.Error(code=2, reason="protocol error"), "unreachable"),
    ],
    ids=["timeout", "gaierror", "refused", "other-oserror", "asyncssh-error"],
)
async def test_handler_maps_connect_exceptions_to_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_reason: str,
) -> None:
    """Every connect/handshake failure maps to a reason code, never re-raises."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.5")

    async def _raise(*_a: object, **_kw: object) -> object:
        raise exc

    monkeypatch.setattr(net_ssh.asyncssh, "get_server_host_key", _raise)
    result = await net_ssh_keyscan(_make_operator(), None, {"host": "203.0.113.5", "port": 22})
    assert result["scanned"] is False
    assert result["reason"] == expected_reason
    assert result["host"] == "203.0.113.5"
    assert result["port"] == 22
    assert result["keys"] == []
    assert result["known_hosts"] == ""


async def test_key_exchange_failed_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-type KeyExchangeFailed skips that type; a later type still scans.

    First requested type raises KeyExchangeFailed (server offers no such
    type), the second returns a real key — the handler collects the second
    rather than aborting on the first.
    """
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.5")
    good = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
    calls: list[list[str]] = []

    async def _fake(host: str, *, port: int, server_host_key_algs: list[str], **_kw: object):
        calls.append(server_host_key_algs)
        if "ssh-ed25519" in server_host_key_algs:
            raise asyncssh.KeyExchangeFailed("no matching host key")
        return good

    monkeypatch.setattr(net_ssh.asyncssh, "get_server_host_key", _fake)
    result = await net_ssh_keyscan(
        _make_operator(),
        None,
        {"host": "203.0.113.5", "key_types": ["ssh-ed25519", "ssh-rsa"]},
    )
    assert result["scanned"] is True
    assert [k["type"] for k in result["keys"]] == [good.get_algorithm()]
    # Both types were attempted (the first was skipped, not fatal).
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Registration + classification + schema conformance
# ---------------------------------------------------------------------------


async def test_ssh_keyscan_registered_as_safe_ungated_typed_op(
    _registered_ssh_keyscan_op: None,
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "net",
                    EndpointDescriptor.version == "1.x",
                    EndpointDescriptor.impl_id == "net-probe",
                    EndpointDescriptor.op_id == _OP_ID,
                )
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_ssh_keyscan_classifies_as_read() -> None:
    from meho_backplane.broadcast.events import classify_op

    assert classify_op("net.ssh_keyscan") == "read"


async def test_success_and_failure_results_validate_against_response_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_ssh_keyscan_op: None,
) -> None:
    """Both a success and a failure body round-trip through the response schema."""
    schema = net_ssh._NET_SSH_KEYSCAN_RESPONSE_SCHEMA
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    acceptor, port, _ = await _start_ssh_server(tmp_path, ["ssh-ed25519"])
    try:
        success = await _dispatch_scan({"host": "127.0.0.1", "port": port})
    finally:
        acceptor.close()
        await acceptor.wait_closed()
    assert success.status == "ok", success.error
    validator.validate(success.result)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    failure = await _dispatch_scan({"host": "127.0.0.1", "port": closed_port})
    assert failure.status == "ok", failure.error
    assert failure.result["scanned"] is False
    validator.validate(failure.result)


# ---------------------------------------------------------------------------
# Pure-helper unit coverage
# ---------------------------------------------------------------------------


def test_resolve_key_types_defaults_dedups_and_filters() -> None:
    assert net_ssh._resolve_key_types(None) == net_ssh._DEFAULT_KEY_TYPES
    assert net_ssh._resolve_key_types([]) == net_ssh._DEFAULT_KEY_TYPES
    # De-duplicated, order preserved.
    assert net_ssh._resolve_key_types(["ssh-rsa", "ssh-rsa", "ssh-ed25519"]) == (
        "ssh-rsa",
        "ssh-ed25519",
    )
    # Unknown entries are dropped; an all-unknown list falls back to defaults.
    assert net_ssh._resolve_key_types(["bogus"]) == net_ssh._DEFAULT_KEY_TYPES


def test_known_hosts_line_uses_bracket_form_only_for_non_default_port() -> None:
    assert (
        net_ssh._known_hosts_line("h.local", 22, "ssh-ed25519", "AAAA")
        == "h.local ssh-ed25519 AAAA"
    )
    assert (
        net_ssh._known_hosts_line("h.local", 2222, "ssh-ed25519", "AAAA")
        == "[h.local]:2222 ssh-ed25519 AAAA"
    )
