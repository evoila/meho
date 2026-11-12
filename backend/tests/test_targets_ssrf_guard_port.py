# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Allowlist entries that carry a port (``host:port``).

A bare entry admits a destination on any port. An entry with a port admits
it on that port only. The port is known from a value embedded in the stored
host, or from the caller (the HTTP dial path and the database/SSH paths pass
the target's port). Where no port is known, an entry with a port never
matches, so the guard stays closed.
"""

from __future__ import annotations

import ipaddress
import types
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.targets import _screen_sent_destination
from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors.adapters.http import (
    HttpConnector,
    SsrfBlockedError,
    _pin_target_addresses,
    _target_dial_port,
)
from meho_backplane.targets.schemas import TargetCreate, TargetUpdate
from meho_backplane.targets.ssrf_guard import (
    TARGET_SSRF_ALLOWLIST_ENV,
    TargetDestinationBlockedError,
    assert_public_destination,
    is_allowlisted,
    screen_and_resolve,
)

_HOST = "svc.lab.internal"


@pytest.fixture(autouse=True)
def _guard_live(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Run the guard unexempted, with the name resolving to private space."""
    monkeypatch.delenv(TARGET_SSRF_ALLOWLIST_ENV, raising=False)
    addrs = [ipaddress.ip_address("10.20.30.40")]
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: addrs)
    return monkeypatch


def _allow(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(TARGET_SSRF_ALLOWLIST_ENV, value)


def test_host_port_entry_admits_only_that_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_and_resolve(_HOST, 8443) is None
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(_HOST, 9443)


def test_bare_host_entry_still_admits_any_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, _HOST)
    for port in (None, 22, 443, 8443):
        assert screen_and_resolve(_HOST, port) is None


def test_host_port_entry_never_matches_an_unknown_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(_HOST)


def test_host_port_entry_is_case_insensitive_and_ignores_trailing_dot(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, "Svc.Lab.Internal.:8443")
    assert screen_and_resolve(_HOST, 8443) is None


def test_port_embedded_in_the_stored_host_is_the_dialed_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_and_resolve(f"{_HOST}:8443") is None
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:9443")
    # The embedded port wins over the caller's port.
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination(f"{_HOST}:9443", 8443)


def test_other_host_is_not_admitted_by_a_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("other.lab.internal", 8443)


def test_ip_with_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "192.168.7.10:8443")
    assert_public_destination("192.168.7.10", 8443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.10", 443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.10")
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("192.168.7.11", 8443)


def test_ipv6_with_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "[fd00::5]:8443")
    assert_public_destination("fd00::5", 8443)
    assert_public_destination("[fd00::5]", 8443)
    with pytest.raises(TargetDestinationBlockedError):
        assert_public_destination("fd00::5", 443)


def test_cidr_range_stays_any_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, "10.0.0.0/8")
    assert_public_destination("10.1.2.3", 22)
    assert_public_destination("10.1.2.3")


@pytest.mark.parametrize(
    "entry", [f"{_HOST}:0", f"{_HOST}:65536", f"{_HOST}:http", "10.0.0.5:99999"]
)
def test_bad_port_in_an_entry_fails_loud(_guard_live: pytest.MonkeyPatch, entry: str) -> None:
    _allow(_guard_live, entry)
    with pytest.raises(ValueError, match="port"):
        assert_public_destination(_HOST, 443)


def test_is_allowlisted_with_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443,192.168.7.10:8443")
    assert is_allowlisted(_HOST, 8443)
    assert is_allowlisted("192.168.7.10", 8443)
    assert not is_allowlisted(_HOST, 443)
    assert not is_allowlisted(_HOST)
    assert not is_allowlisted("192.168.7.10", 443)


def test_target_create_checks_the_embedded_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    created = TargetCreate(name="t1", product="vcenter", host=f"{_HOST}:8443")
    assert created.host == f"{_HOST}:8443"
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=f"{_HOST}:9443")


def test_create_uses_the_target_port_for_a_port_entry(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    created = TargetCreate(name="t1", product="vcenter", host=_HOST, port=8443)
    assert created.port == 8443
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=_HOST, port=9443)
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host=_HOST)
    with pytest.raises(ValidationError):
        TargetCreate(name="t1", product="vcenter", host="other.lab.internal", port=8443, fqdn=_HOST)


def test_update_with_a_body_port_uses_that_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert TargetUpdate(host=_HOST, port=8443).host == _HOST
    with pytest.raises(ValidationError):
        TargetUpdate(host=_HOST, port=9443)


def test_update_without_a_body_port_uses_the_stored_port(
    _guard_live: pytest.MonkeyPatch,
) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    body = TargetUpdate(host=_HOST)  # the schema defers: the stored port is unknown here
    updates = body.model_dump(exclude_unset=True)
    _screen_sent_destination(updates, types.SimpleNamespace(port=8443))  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as excinfo:
        _screen_sent_destination(updates, types.SimpleNamespace(port=9443))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422


# --- connect time -----------------------------------------------------------


def test_dial_guard_passes_a_port_entry_through_by_name(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert screen_dial_addresses(_HOST, 8443) is None
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses(_HOST, 9443)
    with pytest.raises(TargetDestinationBlockedError):
        screen_dial_addresses(_HOST)


class _Probe(HttpConnector):
    product = "probe"

    async def auth_headers(self, target: Any, operator: Any) -> dict[str, str]:
        return {}

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


def _target(host: str, port: int | None) -> Any:
    return types.SimpleNamespace(
        name="ssrf-port-target",
        host=host,
        port=port,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


def test_target_dial_port_uses_the_target_port_or_the_scheme_default() -> None:
    assert _target_dial_port(_target(_HOST, 8443)) == 8443
    assert _target_dial_port(_target(_HOST, None)) == 443


async def test_http_connect_admits_the_listed_port_only(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    connector = _Probe()
    try:
        client = await connector._http_client(_target(_HOST, 8443))
        assert isinstance(client, httpx.AsyncClient)
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(_target(_HOST, 9443))
        with pytest.raises(SsrfBlockedError):
            await connector._http_client(_target(_HOST, None))
    finally:
        await connector.aclose()


def test_pinned_resolver_gets_the_port(_guard_live: pytest.MonkeyPatch) -> None:
    _allow(_guard_live, f"{_HOST}:8443")
    assert _pin_target_addresses(_HOST, port=8443) is None
    with pytest.raises(SsrfBlockedError):
        _pin_target_addresses(_HOST, port=9443)
