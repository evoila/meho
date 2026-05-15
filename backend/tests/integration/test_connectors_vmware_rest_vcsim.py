# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration test for :class:`VmwareRestConnector` against a real vcsim container.

Boots ``vmware/vcsim`` via :class:`testcontainers.core.container.DockerContainer`,
configures it for HTTPS on the listening port, and exercises the live
``fingerprint`` / ``probe`` / session-cache / ``aclose`` paths against the
running simulator's REST surface.

Skip conditions:

* Docker socket missing — same heuristic the rest of the
  ``tests/integration/`` package uses.
* vcsim container start fails — surfaces as a clean skip rather than a
  hard failure so a Docker-having-but-not-vcsim-having sandbox isn't
  flagged red. CI runners provision Docker so the tests run there.

CI side: the vcsim integration job lands as part of T8 (#515); this
module collects unconditionally so a stub CI lane that mounts the
Docker socket without setting up the vcsim image still goes to skip
rather than collection-fail.

vcsim notes:

* The official ``vmware/vcsim:latest`` image listens on
  ``127.0.0.1:8989`` by default with HTTPS (self-signed cert).
* The simulator accepts any ``username``/``password`` for
  ``POST /api/session`` — vcsim returns a synthetic token without
  verifying credentials. We pass ``user``/``pass`` for clarity; any
  pair works.
* ``GET /api/about`` returns a synthesised inventory shape that maps
  cleanly through :func:`product_from_line_id`.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    VsphereTargetLike,
)

# ---------------------------------------------------------------------------
# Docker availability — mirrors tests/integration/conftest.py heuristic
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


# ---------------------------------------------------------------------------
# Target stub
# ---------------------------------------------------------------------------


@dataclass
class _VcsimTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value


# ---------------------------------------------------------------------------
# vcsim container fixture — module-scoped (one boot, multiple tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vcsim_target() -> Any:
    """Boot a vcsim container; yield a target pointing at it.

    Container shut down on fixture teardown. vcsim listens on 8989/tcp
    inside the container; testcontainers maps that to an ephemeral host
    port we read via :meth:`DockerContainer.get_exposed_port`.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local import — testcontainers transitively imports the docker SDK
    # which probes the socket on import. Keeping the import inside the
    # fixture lets the module collect on a no-Docker sandbox.
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"testcontainers unavailable: {exc}")

    # ``vmware/vcsim:latest`` is the upstream image. Override via env var
    # for registry-mirror swaps or version pinning per the same convention
    # the K3s integration test uses (MEHO_TEST_K3S_IMAGE).
    image = os.environ.get("MEHO_TEST_VCSIM_IMAGE", "vmware/vcsim:latest")
    # The official ``vmware/vcsim`` Dockerfile sets
    # ``ENTRYPOINT ["/vcsim"]`` + ``CMD ["-l", "0.0.0.0:8989"]`` so the
    # default listener already binds on all interfaces — exactly what
    # testcontainers' host-port mapping needs to reach the simulator.
    # Overriding ``CMD`` with a string here would re-prepend ``/vcsim``
    # to the entrypoint and Go's ``flag.Parse()`` would stop at that
    # positional argument, silently falling back to the simulator's
    # internal default of ``-l 127.0.0.1:8989`` — bound to container-
    # local loopback only, so the host's mapped port would refuse the
    # TCP connect with an empty ``ConnectError`` (the failure mode that
    # took down PR #518's first CI green attempt). Leave the default
    # CMD alone.
    container = DockerContainer(image).with_exposed_ports(8989)
    import contextlib

    try:
        container.start()
        # The simulator logs "export GOVC_URL=..." once the REST endpoint
        # is ready to serve. Wait up to 30 s for that line; longer would
        # mask a real boot failure as a flaky test.
        wait_for_logs(container, "export GOVC_URL", timeout=30)
    except Exception as exc:
        # Best-effort tear-down; if the container never started, .stop()
        # will itself fail — swallow so the user sees the original boot
        # exception via pytest.skip, not the cleanup secondary.
        with contextlib.suppress(Exception):
            container.stop()
        pytest.skip(f"vcsim container failed to start ({type(exc).__name__}): {exc}")

    try:
        host = container.get_container_host_ip()
        port_str = container.get_exposed_port(8989)
        target = _VcsimTarget(
            name="vcsim-test",
            host=host,
            port=int(port_str),
            secret_ref="kv/data/vsphere/vcsim-test",
        )
        yield target
    finally:
        container.stop()


@pytest.fixture
async def vcsim_connector(
    vcsim_target: _VcsimTarget,
) -> AsyncIterator[tuple[VmwareRestConnector, _VcsimTarget]]:
    """Yield a connector wired with a loader that returns vcsim's any-credentials pair.

    Also patches the connector's per-target httpx client constructor to
    accept the simulator's self-signed cert. The patch is scoped to this
    fixture so production code (which uses httpx's default TLS
    verification) stays untouched.
    """

    async def _loader(_target: VsphereTargetLike) -> dict[str, str]:
        # vcsim accepts any credentials.
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    # Override the parent's _http_client to build a client with TLS
    # verification disabled — the self-signed cert vcsim ships isn't
    # trusted by the host's CA bundle. Production code never reaches
    # this branch; the override is fixture-scoped via a method-replace
    # so the per-target dict pooling semantics stay intact.
    import asyncio

    connector._lock_for_test = asyncio.Lock()  # type: ignore[attr-defined]

    async def _http_client_insecure(target: VsphereTargetLike) -> httpx.AsyncClient:
        async with connector._lock_for_test:  # type: ignore[attr-defined]
            if target.name not in connector._clients:
                scheme = "https"
                port_part = f":{target.port}" if target.port and target.port != 443 else ""
                base_url = f"{scheme}://{target.host}{port_part}"
                # vcsim's self-signed cert isn't in the host CA bundle;
                # disable verification for this fixture only. Production
                # code uses httpx's default verify=True.
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                connector._clients[target.name] = httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                    verify=ssl_ctx,
                )
            return connector._clients[target.name]

    connector._http_client = _http_client_insecure  # type: ignore[method-assign]

    try:
        yield connector, vcsim_target
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_against_vcsim_returns_reachable(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """Live fingerprint against vcsim returns reachable=True with vmware vendor."""
    connector, target = vcsim_connector
    result = await connector.fingerprint(target)
    assert result.vendor == "vmware"
    assert result.reachable is True, f"fingerprint not reachable: extras={dict(result.extras)}"
    assert result.probe_method == "GET /api/about"
    # vcsim's /api/about returns product_line_id="vpx" -> "vcenter".
    # Loose assertion since vcsim's behaviour may vary across releases.
    assert result.product in ("vcenter", "esxi", "vpx", "embeddedEsx", "esx")


@pytest.mark.asyncio
async def test_probe_against_vcsim_returns_ok(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """probe() against a running vcsim returns ok=True."""
    connector, target = vcsim_connector
    result = await connector.probe(target)
    assert result.ok is True, f"probe failed: reason={result.reason!r}"


@pytest.mark.asyncio
async def test_session_reused_across_consecutive_fingerprint_calls(
    vcsim_connector: tuple[VmwareRestConnector, _VcsimTarget],
) -> None:
    """Two consecutive fingerprint calls share the same cached session token."""
    connector, target = vcsim_connector
    await connector.fingerprint(target)
    token_after_first = connector._session_tokens.get(target.name)
    assert token_after_first is not None
    await connector.fingerprint(target)
    token_after_second = connector._session_tokens.get(target.name)
    # The load-bearing assertion: the cached token is byte-identical
    # across the two calls (no re-establish).
    assert token_after_first == token_after_second


@pytest.mark.asyncio
async def test_aclose_revokes_session_against_vcsim(
    vcsim_target: _VcsimTarget,
) -> None:
    """aclose() issues DELETE /api/session against vcsim and clears the cache."""

    async def _loader(_target: VsphereTargetLike) -> dict[str, str]:
        return {"username": "user", "password": "pass"}

    connector = VmwareRestConnector(session_loader=_loader)

    # Same insecure-client patch as the fixture.
    import asyncio

    connector._lock_for_test = asyncio.Lock()  # type: ignore[attr-defined]

    async def _http_client_insecure(target: VsphereTargetLike) -> httpx.AsyncClient:
        async with connector._lock_for_test:  # type: ignore[attr-defined]
            if target.name not in connector._clients:
                port_part = f":{target.port}" if target.port and target.port != 443 else ""
                base_url = f"https://{target.host}{port_part}"
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                connector._clients[target.name] = httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                    verify=ssl_ctx,
                )
            return connector._clients[target.name]

    connector._http_client = _http_client_insecure  # type: ignore[method-assign]

    await connector.fingerprint(vcsim_target)
    assert vcsim_target.name in connector._session_tokens

    await connector.aclose()
    # Post-aclose: cache cleared, pool emptied.
    assert connector._session_tokens == {}
    assert connector._clients == {}
