# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Static-kubeconfig targets dial ``target.host`` and honour ``tls_server_name``.

A static kubeconfig embeds the cluster's own ``server``. For a guest
cluster behind a NAT alias that ``server`` is a workload-network VIP the
backplane cannot route, so dialing it hangs / raises
``ClientConnectorError``. The connector registers such a target with an
operator-reachable ``host`` (the alias) plus a ``tls_server_name`` naming
the cert's SAN; the client-build path must dial that ``host`` and verify
the presented cert against the SAN, exactly as the WCP-SSO path already
does for a vSphere Supervisor.

This module pins:

* :func:`_kubeconfig_dialing_target` -- the pure rewrite: ``server`` ->
  ``https://{host}:{port}``, ``tls-server-name`` <- ``tls_server_name``,
  ``insecure-skip-tls-verify`` <- ``not verify_tls``, embedded CA kept,
  input never mutated, and the no-host / unresolvable-context pass-through.
* the built :class:`~kubernetes_asyncio.client.Configuration` end to end
  through :meth:`KubernetesConnector._get_api_client` (REST) and
  :meth:`KubernetesConnector._get_ws_api_client` (exec/websocket): the
  ``host`` is the target URL (not the kubeconfig server) and
  ``tls_server_name`` is applied; a host-less target is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector, KubernetesTargetLike
from meho_backplane.connectors.kubernetes.connector import (
    _DEFAULT_K8S_PORT,
    _active_cluster_name,
    _kubeconfig_dialing_target,
)

# A raw internal-VIP address the backplane cannot route -- the value the
# rewrite must replace with the operator-reachable target host.
_EMBEDDED_SERVER = "https://10.99.99.99:6443"
# base64("dummy-ca") -- never a real trust anchor; only the end-to-end
# tests that must build a real client omit it (system store is enough).
_CA_DATA = "ZHVtbXktY2E="


@dataclass
class _StubTarget:
    """Structural :class:`KubernetesTargetLike` + the TLS knobs read via ``getattr``."""

    name: str
    host: str
    port: int | None
    secret_ref: str
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    id: object = field(default_factory=uuid4)
    tenant_id: object = field(default_factory=lambda: UUID(int=0))


def _kubeconfig(*, server: str = _EMBEDDED_SERVER, ca_data: str | None = None) -> dict[str, Any]:
    cluster: dict[str, Any] = {"server": server}
    if ca_data is not None:
        cluster["certificate-authority-data"] = ca_data
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "ctx",
        "contexts": [{"name": "ctx", "context": {"cluster": "c1", "user": "u1"}}],
        "clusters": [{"name": "c1", "cluster": cluster}],
        "users": [{"name": "u1", "user": {"token": "stub-token"}}],
    }


def _make_operator() -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


def _connector_for(kubeconfig: dict[str, Any]) -> KubernetesConnector:
    async def _loader(target: KubernetesTargetLike, operator: Operator) -> dict[str, Any]:
        del target, operator
        return kubeconfig

    return KubernetesConnector(kubeconfig_loader=_loader)


def _active_cluster(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Return the active context's cluster mapping (test-side resolver)."""
    name = _active_cluster_name(config_dict)
    assert name is not None
    for entry in config_dict["clusters"]:
        if entry["name"] == name:
            return entry["cluster"]
    raise AssertionError(f"cluster {name!r} not in kubeconfig")


# ---------------------------------------------------------------------------
# _kubeconfig_dialing_target -- the pure rewrite
# ---------------------------------------------------------------------------


def test_rewrite_points_active_cluster_at_target_host() -> None:
    target = _StubTarget(
        name="guest",
        host="guest.alias.test",  # the operator-reachable NAT alias
        port=6443,
        secret_ref="k8s/guest",
        tls_server_name="guest-vip.test",  # the cert SAN
    )
    rewritten = _kubeconfig_dialing_target(_kubeconfig(ca_data=_CA_DATA), target)
    cluster = _active_cluster(rewritten)

    # Dials the reachable alias, not the embedded internal VIP.
    assert cluster["server"] == "https://guest.alias.test:6443"
    # Verifies the presented cert against the SAN (hostname checking on).
    assert cluster["tls-server-name"] == "guest-vip.test"
    assert cluster["insecure-skip-tls-verify"] is False
    # The embedded trust anchor is preserved.
    assert cluster["certificate-authority-data"] == _CA_DATA


def test_rewrite_defaults_port_when_target_port_is_none() -> None:
    target = _StubTarget(name="guest", host="guest.alias.test", port=None, secret_ref="k8s/guest")
    cluster = _active_cluster(_kubeconfig_dialing_target(_kubeconfig(), target))
    assert cluster["server"] == f"https://guest.alias.test:{_DEFAULT_K8S_PORT}"


def test_rewrite_omits_tls_server_name_when_target_has_none() -> None:
    target = _StubTarget(name="guest", host="guest.alias.test", port=6443, secret_ref="k8s/guest")
    cluster = _active_cluster(_kubeconfig_dialing_target(_kubeconfig(), target))
    # No override -> verification falls back to the dial host, so the key
    # is not injected.
    assert "tls-server-name" not in cluster


def test_rewrite_verify_tls_false_sets_insecure_skip() -> None:
    target = _StubTarget(
        name="guest", host="guest.alias.test", port=6443, secret_ref="k8s/guest", verify_tls=False
    )
    cluster = _active_cluster(_kubeconfig_dialing_target(_kubeconfig(ca_data=_CA_DATA), target))
    assert cluster["insecure-skip-tls-verify"] is True
    # verify_tls=false does not discard the embedded CA -- it only flips
    # verification off, matching the WCP path.
    assert cluster["certificate-authority-data"] == _CA_DATA


def test_rewrite_no_host_returns_input_unchanged() -> None:
    target = _StubTarget(name="appliance", host="", port=6443, secret_ref="k8s/appliance")
    original = _kubeconfig()
    result = _kubeconfig_dialing_target(original, target)
    # Same object identity: a host-less target takes the untouched path.
    assert result is original
    assert _active_cluster(result)["server"] == _EMBEDDED_SERVER


def test_rewrite_unresolvable_context_returns_input_unchanged() -> None:
    target = _StubTarget(name="guest", host="guest.alias.test", port=6443, secret_ref="k8s/guest")
    broken = _kubeconfig()
    broken["current-context"] = "does-not-exist"
    result = _kubeconfig_dialing_target(broken, target)
    assert result is broken
    assert _active_cluster_name(broken) is None


def test_rewrite_does_not_mutate_the_input_dict() -> None:
    target = _StubTarget(
        name="guest",
        host="guest.alias.test",
        port=6443,
        secret_ref="k8s/guest",
        tls_server_name="guest-vip.test",
    )
    original = _kubeconfig(ca_data=_CA_DATA)
    _kubeconfig_dialing_target(original, target)
    # The credential's own dict is never touched -- only a copy is rewritten.
    embedded = original["clusters"][0]["cluster"]
    assert embedded["server"] == _EMBEDDED_SERVER
    assert "tls-server-name" not in embedded
    assert "insecure-skip-tls-verify" not in embedded


# ---------------------------------------------------------------------------
# End-to-end -- the built Configuration (REST + ws/exec)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rest_client_dials_target_host_and_applies_tls_server_name() -> None:
    target = _StubTarget(
        name="guest",
        host="guest.alias.test",
        port=6443,
        secret_ref="k8s/guest",
        tls_server_name="guest-vip.test",
    )
    connector = _connector_for(_kubeconfig())
    api_client = await connector._get_api_client(target, _make_operator())
    try:
        cfg = api_client.configuration
        assert cfg.host == "https://guest.alias.test:6443"
        assert cfg.host != _EMBEDDED_SERVER
        assert cfg.tls_server_name == "guest-vip.test"
        assert cfg.verify_ssl is True
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_rest_client_without_host_keeps_embedded_server() -> None:
    target = _StubTarget(name="appliance", host="", port=6443, secret_ref="k8s/appliance")
    connector = _connector_for(_kubeconfig())
    api_client = await connector._get_api_client(target, _make_operator())
    try:
        # No host -> the kubeconfig's embedded server is used unchanged.
        assert api_client.configuration.host == _EMBEDDED_SERVER
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_ws_client_dials_target_host_and_applies_tls_server_name() -> None:
    target = _StubTarget(
        name="guest",
        host="guest.alias.test",
        port=6443,
        secret_ref="k8s/guest",
        tls_server_name="guest-vip.test",
    )
    connector = _connector_for(_kubeconfig())
    ws_client = await connector._get_ws_api_client(target, _make_operator())
    try:
        cfg = ws_client.configuration
        assert cfg.host == "https://guest.alias.test:6443"
        assert cfg.tls_server_name == "guest-vip.test"
        assert cfg.verify_ssl is True
    finally:
        await connector.aclose()
