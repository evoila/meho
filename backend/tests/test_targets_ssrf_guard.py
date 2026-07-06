# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the target SSRF guard (evoila-bosnia/meho-internal#153).

Coverage matrix (per Task acceptance criteria):

* :class:`TargetCreate` rejects ``127.0.0.1``, ``10.0.0.1``,
  ``192.168.1.1``, ``169.254.169.254``, and IPv6 ``::1`` hosts with a
  pydantic :class:`ValidationError` (the FastAPI 422 shape) when the
  allowlist is empty; a public host still validates.
* :class:`TargetUpdate` rejects the same shapes on ``host`` (and
  ``fqdn``); the all-``None`` PATCH body stays valid.
* The connect path (``HttpConnector._http_client``) re-screens the
  **resolved** address: a hostname resolving into private / metadata
  space raises :class:`SsrfBlockedError` before any client is built or
  request issued (resolver monkeypatched — no real DNS).
* ``MEHO_TARGET_SSRF_ALLOWLIST`` exempts CIDR ranges and hostname
  literals at both layers; with the allowlist empty the same target is
  rejected.
* The rejection message never echoes the resolved address (no
  internal-topology oracle) and is excluded from the transport retry
  policy (deterministic verdict).

The suite-wide autouse fixture ``_default_target_ssrf_allowlist``
(``conftest.py``) pins a permissive allowlist + a no-op resolver for the
legacy fixture corpus; every test here explicitly clears / re-pins both,
so the guard is exercised for real.
"""

from __future__ import annotations

import ipaddress
import types
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import (
    HttpConnector,
    SsrfBlockedError,
    _retryable,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
    assert_public_destination,
)

# 93.184.216.34 (example.com's long-stable A record) — a public, globally
# routable literal that must always pass the guard.
_PUBLIC_IP = "93.184.216.34"

_NON_PUBLIC_HOSTS = [
    "127.0.0.1",
    "10.0.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "::1",
]


@pytest.fixture
def _guard_live(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear the suite-wide allowlist so the guard runs unexempted."""
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    return monkeypatch


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, *ips: str) -> None:
    """Point the guard's DNS seam at a fixed answer (no real DNS)."""
    addrs = [ipaddress.ip_address(ip) for ip in ips]
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: addrs)


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "t1", "product": "vcenter", "host": _PUBLIC_IP}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Create/update boundary (schema validators)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", _NON_PUBLIC_HOSTS)
def test_target_create_rejects_non_public_host_literal(
    _guard_live: pytest.MonkeyPatch, host: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TargetCreate(**_create_kwargs(host=host))
    assert "not a public address" in str(excinfo.value)


@pytest.mark.parametrize("host", _NON_PUBLIC_HOSTS)
def test_target_update_rejects_non_public_host_literal(
    _guard_live: pytest.MonkeyPatch, host: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TargetUpdate(host=host)
    assert "not a public address" in str(excinfo.value)


def test_target_create_rejects_non_public_fqdn(_guard_live: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(fqdn="169.254.169.254"))


def test_target_create_accepts_public_host(_guard_live: pytest.MonkeyPatch) -> None:
    target = TargetCreate(**_create_kwargs())
    assert target.host == _PUBLIC_IP


def test_target_create_accepts_publicly_resolving_hostname(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(_guard_live, _PUBLIC_IP)
    target = TargetCreate(**_create_kwargs(host="vcenter.example.com"))
    assert target.host == "vcenter.example.com"


def test_target_create_rejects_hostname_resolving_private(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    """The *resolved* address is screened, not just the stored literal."""
    _patch_resolver(_guard_live, "10.20.30.40")
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(host="benign-looking.example.com"))


def test_rejection_message_does_not_echo_resolved_address(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    """No internal-topology oracle: the resolved IP never leaves the guard."""
    _patch_resolver(_guard_live, "10.20.30.40")
    with pytest.raises(TargetDestinationBlockedError) as excinfo:
        assert_public_destination("benign-looking.example.com")
    assert "10.20.30.40" not in str(excinfo.value)
    assert TARGET_SSRF_ALLOWLIST_ENV in str(excinfo.value)


def test_unresolvable_hostname_passes_at_create(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    """Fail-open on NXDOMAIN by design — connect re-checks every dispatch."""
    _patch_resolver(_guard_live)  # resolver returns no addresses
    target = TargetCreate(**_create_kwargs(host="vcenter.invalid"))
    assert target.host == "vcenter.invalid"


def test_target_update_all_none_still_valid(_guard_live: pytest.MonkeyPatch) -> None:
    update = TargetUpdate()
    assert update.host is None


# ---------------------------------------------------------------------------
# Allowlist override (env-driven)
# ---------------------------------------------------------------------------


def test_allowlist_cidr_permits_otherwise_blocked_range(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "10.0.0.0/8")
    target = TargetCreate(**_create_kwargs(host="10.0.0.1"))
    assert target.host == "10.0.0.1"
    # The exemption is range-scoped, not a global off-switch.
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(host="192.168.1.1"))


def test_empty_allowlist_rejects_same_target(_guard_live: pytest.MonkeyPatch) -> None:
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "")
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(host="10.0.0.1"))


def test_allowlist_hostname_entry_permits_private_resolution(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(_guard_live, "10.20.30.40")
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "vcenter.lab.internal")
    target = TargetCreate(**_create_kwargs(host="vcenter.lab.internal"))
    assert target.host == "vcenter.lab.internal"
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(host="other.lab.internal"))


def test_allowlist_bare_ip_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "192.168.7.10")
    TargetCreate(**_create_kwargs(host="192.168.7.10"))
    with pytest.raises(ValidationError):
        TargetCreate(**_create_kwargs(host="192.168.7.11"))


def test_malformed_allowlist_cidr_fails_loud(_guard_live: pytest.MonkeyPatch) -> None:
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "10.0.0.0/99")
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        TargetCreate(**_create_kwargs(host="10.0.0.1"))
    assert TARGET_SSRF_ALLOWLIST_ENV in str(excinfo.value)


# ---------------------------------------------------------------------------
# Connect path (HttpConnector._http_client)
# ---------------------------------------------------------------------------


class _GuardProbeConnector(HttpConnector):
    """Minimal concrete subclass — overrides auth_headers + ABC methods."""

    product = "test-ssrf"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


def _make_target(host: str) -> Any:
    return types.SimpleNamespace(
        name="ssrf-target",
        host=host,
        port=443,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
    )


def _make_operator() -> Operator:
    from uuid import UUID

    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def test_connect_refuses_hostname_resolving_private(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    """Resolved-IP re-check at dispatch: refused before any client exists."""
    _patch_resolver(_guard_live, "10.20.30.40")
    connector = _GuardProbeConnector()
    with pytest.raises(SsrfBlockedError):
        await connector._http_client(_make_target("benign-looking.example.com"))
    assert connector._clients == {}


async def test_connect_refuses_metadata_resolution_and_issues_no_request(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(_guard_live, "169.254.169.254")
    connector = _GuardProbeConnector()
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://metadata.example.com/api").respond(200, json={})
        with pytest.raises(SsrfBlockedError):
            await connector._request_json(
                _make_target("metadata.example.com"),
                "GET",
                "/api",
                operator=_make_operator(),
            )
    assert route.call_count == 0
    assert connector._clients == {}


async def test_connect_refuses_private_ip_literal(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    connector = _GuardProbeConnector()
    with pytest.raises(SsrfBlockedError):
        await connector._http_client(_make_target("10.0.0.7"))
    assert connector._clients == {}


async def test_connect_allowlist_permits_and_builds_client(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(_guard_live, "169.254.169.254")
    _guard_live.setenv(TARGET_SSRF_ALLOWLIST_ENV, "169.254.0.0/16")
    connector = _GuardProbeConnector()
    try:
        client = await connector._http_client(_make_target("metadata.example.com"))
        assert isinstance(client, httpx.AsyncClient)
        assert len(connector._clients) == 1
    finally:
        await connector.aclose()


async def test_connect_recheck_catches_post_create_rebind(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    """A hostname that was public (or unresolvable) at create time is
    still refused the moment its DNS answer moves into private space."""
    _patch_resolver(_guard_live, _PUBLIC_IP)
    connector = _GuardProbeConnector()
    target = _make_target("rebinding.example.com")
    try:
        await connector._http_client(target)  # public answer: client pooled
        _patch_resolver(_guard_live, "10.20.30.40")  # DNS now rebinds
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(target)
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


def test_ssrf_blocked_error_is_connect_error_but_not_retryable() -> None:
    err = SsrfBlockedError("blocked")
    assert isinstance(err, httpx.ConnectError)
    assert _retryable(err) is False
