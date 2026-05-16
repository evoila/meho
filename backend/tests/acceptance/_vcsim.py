# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcsim endpoint resolver for the G3.1 vmware-rest acceptance suite.

vcsim is the VMware-shipped govmomi-backed vCenter simulator. The
``vmware/vcsim`` Docker image listens on ``0.0.0.0:8989`` by default
and serves both ``/api`` (new REST surface) and ``/rest`` (legacy
mount), accepting any HTTP basic credential — the simulator never
validates the username/password pair. That property lets the
acceptance suite drive the real :class:`VmwareRestConnector` (#498)
through ``POST /api/session`` / ``GET /api/about`` /
``GET /vcenter/*`` without standing up a real vCenter.

Resolution priority
===================

The :func:`resolve_vcsim_endpoint` helper consults sources in this
order so an operator with a pre-provisioned vcsim instance (e.g. a
shared lab daemon, the CI runner's `vcsim` service container) can
bypass the testcontainers boot:

1. **``MEHO_VCSIM_URL``** — explicit base URL pointing at a running
   vcsim instance (e.g. ``https://10.0.5.4:8989``). The helper
   parses the URL and returns an endpoint without booting a
   container. This is the CI path for the meho-runners pool where
   a vcsim daemon is provisioned alongside the runner.
2. **testcontainers-managed ``vmware/vcsim`` boot** — when no env
   override resolves, the helper boots the public ``vmware/vcsim``
   image with the seed flags described under "Seeding the topology"
   and yields an endpoint targeting the host-mapped port.

Either path yields a :class:`VcsimEndpoint` carrying the host, the
mapped port, the no-auth credential pair, and a base URL string the
:class:`VmwareRestConnector` can dial directly. The endpoint is
session-scoped from the conftest fixture so the ~3-second container
boot only pays once per pytest invocation.

Seeding the topology
====================

vcsim accepts CLI flags at startup to seed its in-memory inventory:

- ``-vm 50`` — 50 simulated virtual machines.
- ``-host 3`` — 3 ESXi host objects.
- ``-cluster 1`` — 1 cluster (the 3 hosts roll up to it).
- ``-ds 2`` — 2 datastores.
- ``-folder 2`` — 2 folders, useful for resolver-ambiguity tests.

These match the canary's "50 VMs / 3 hosts / 1 cluster / 2 ds"
fixture topology so the JSONFlux force-mode test can rely on
``GET:/vcenter/vm`` returning exactly 50 rows. The defaults are
overridable per-test via :func:`build_vcsim_command` for callers
that need a sparser or denser inventory.

Reference: https://github.com/vmware/govmomi/tree/main/vcsim
"""

from __future__ import annotations

import contextlib
import os
import ssl
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

__all__ = [
    "DEFAULT_VCSIM_PASSWORD",
    "DEFAULT_VCSIM_TOPOLOGY",
    "DEFAULT_VCSIM_USERNAME",
    "VCSIM_DOCKER_SKIP_REASON",
    "VCSIM_PORT",
    "VcsimEndpoint",
    "VcsimTopology",
    "build_vcsim_command",
    "build_vcsim_http_client",
    "patch_vmware_connector_for_vcsim",
    "resolve_vcsim_endpoint",
]


#: vcsim's listening port inside the container.
VCSIM_PORT: int = 8989

#: vcsim accepts any credentials — these are the conventional values
#: used across the acceptance + integration suites for readability.
DEFAULT_VCSIM_USERNAME: str = "user"
DEFAULT_VCSIM_PASSWORD: str = "pass"

#: Reason surfaced when the helper can't resolve nor boot vcsim.
VCSIM_DOCKER_SKIP_REASON: str = (
    "vcsim unavailable: set MEHO_VCSIM_URL to a running vcsim base URL "
    "(e.g. https://10.0.5.4:8989), or run on a host with a Docker socket "
    "so testcontainers can boot vmware/vcsim:latest."
)


@dataclass(frozen=True)
class VcsimTopology:
    """Seed counts the vcsim container is started with.

    Mirrors the CLI flags vcsim accepts: ``-vm``, ``-host``,
    ``-cluster``, ``-ds``, ``-folder``. The acceptance suite's
    default values match the issue body's "50 VMs / 3 hosts / 1
    cluster / 2 ds / 2 folders" so the JSONFlux force-mode test can
    assert exact row counts.
    """

    vms: int = 50
    hosts: int = 3
    clusters: int = 1
    datastores: int = 2
    folders: int = 2


#: Default topology — the JSONFlux force-mode test asserts these exact
#: counts so callers that override the seed must adapt their own
#: assertions.
DEFAULT_VCSIM_TOPOLOGY: VcsimTopology = VcsimTopology()


@dataclass(frozen=True)
class VcsimEndpoint:
    """Resolved vcsim location + no-auth credential pair.

    Carries everything the :class:`VmwareRestConnector` needs to
    dial vcsim:

    - :attr:`host` — hostname or IP (``localhost`` for testcontainers,
      whatever the env URL resolves to for the override path).
    - :attr:`port` — TCP port the simulator listens on.
    - :attr:`scheme` — ``"https"`` for vcsim's self-signed default,
      ``"http"`` for operators who terminate TLS at a proxy.
    - :attr:`username` / :attr:`password` — vcsim accepts anything;
      the constants here keep test code readable.
    - :attr:`base_url` — pre-built ``scheme://host:port`` string the
      :class:`httpx.AsyncClient` constructor accepts.
    """

    host: str
    port: int
    scheme: str
    username: str
    password: str

    @property
    def base_url(self) -> str:
        """Return ``scheme://host:port`` (omits ``:port`` for the default)."""
        if (self.scheme == "https" and self.port == 443) or (
            self.scheme == "http" and self.port == 80
        ):
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"


def build_vcsim_command(
    topology: VcsimTopology = DEFAULT_VCSIM_TOPOLOGY,
    *,
    listen: str = "0.0.0.0:8989",
) -> list[str]:
    """Compose the vcsim CLI args for *topology*.

    Returned as a list so :meth:`DockerContainer.with_command` lands
    each flag as its own ``argv`` entry — passing the string form
    would let Go's ``flag.Parse()`` halt at the first positional and
    silently fall back to ``-l 127.0.0.1:8989`` (the failure mode
    PR #518 hit on its first CI green attempt).

    The ``-l`` flag MUST come first because the container's
    ``ENTRYPOINT`` is ``["/vcsim"]`` — every subsequent arg is a
    seed flag the simulator parses before binding the listener.
    """
    return [
        "-l",
        listen,
        "-vm",
        str(topology.vms),
        "-host",
        str(topology.hosts),
        "-cluster",
        str(topology.clusters),
        "-ds",
        str(topology.datastores),
        "-folder",
        str(topology.folders),
    ]


def _docker_socket_present() -> bool:
    """Return ``True`` iff a usable Docker socket is reachable.

    Mirrors the heuristic at
    :func:`tests.integration.conftest._docker_socket_present` so the
    acceptance suite gates on the same skip condition as every other
    testcontainers-driven test in the project.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


def _endpoint_from_env(raw: str) -> VcsimEndpoint:
    """Parse ``MEHO_VCSIM_URL`` into a :class:`VcsimEndpoint`.

    Accepts bare ``host:port`` (defaults to ``https``), full
    ``scheme://host:port``, or a URL with an explicit path; the path
    is dropped here because callers (the
    :class:`~meho_backplane.connectors.vmware_rest.VmwareRestConnector`
    in particular) supply their own path off the base URL.
    """
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if scheme == "https" else 80)
    return VcsimEndpoint(
        host=host,
        port=port,
        scheme=scheme,
        username=DEFAULT_VCSIM_USERNAME,
        password=DEFAULT_VCSIM_PASSWORD,
    )


@contextlib.contextmanager
def _boot_vcsim_container(
    topology: VcsimTopology,
) -> Iterator[VcsimEndpoint]:
    """Boot ``vmware/vcsim`` via testcontainers; yield its endpoint.

    Yields one endpoint and tears the container down on context
    exit. The simulator emits ``export GOVC_URL=...`` to stdout once
    the REST endpoint is serving; ``wait_for_logs`` blocks on that
    line up to 30 s. Longer would mask a real boot failure as
    flake; shorter risks slow CI runner pulls timing out before
    vcsim is ready.
    """
    # Local imports — testcontainers transitively imports the docker
    # SDK which probes the socket on import. Keeping the import inside
    # the context manager lets the module collect on a no-Docker
    # sandbox (the env-var path doesn't reach here at all).
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    image = os.environ.get("MEHO_TEST_VCSIM_IMAGE", "vmware/vcsim:latest")
    container = (
        DockerContainer(image)
        .with_exposed_ports(VCSIM_PORT)
        .with_command(build_vcsim_command(topology))
    )

    try:
        container.start()
        wait_for_logs(container, "export GOVC_URL", timeout=30)
    except Exception:
        # Best-effort cleanup; surfacing the original boot exception
        # is more useful than the secondary stop() failure.
        with contextlib.suppress(Exception):
            container.stop()
        raise

    try:
        host = container.get_container_host_ip()
        port_str = container.get_exposed_port(VCSIM_PORT)
        yield VcsimEndpoint(
            host=host,
            port=int(port_str),
            scheme="https",
            username=DEFAULT_VCSIM_USERNAME,
            password=DEFAULT_VCSIM_PASSWORD,
        )
    finally:
        container.stop()


@contextlib.contextmanager
def resolve_vcsim_endpoint(
    topology: VcsimTopology = DEFAULT_VCSIM_TOPOLOGY,
) -> Iterator[VcsimEndpoint]:
    """Yield a :class:`VcsimEndpoint`; pick env override or testcontainers boot.

    Context manager so the testcontainers branch can tear the
    simulator down deterministically on exit; the env-override branch
    is a no-op cleanup (the operator manages the lifecycle of the
    pre-provisioned vcsim daemon themselves).

    Raises :class:`pytest.skip.Exception` (via :func:`pytest.skip`)
    when neither path is viable so the calling fixture surfaces the
    skip reason cleanly. The same fixture-level skip pattern the
    integration suite uses for the ``pg_engine`` no-Docker case.
    """
    env_raw = os.environ.get("MEHO_VCSIM_URL", "").strip()
    if env_raw:
        yield _endpoint_from_env(env_raw)
        return

    if not _docker_socket_present():
        pytest.skip(VCSIM_DOCKER_SKIP_REASON)

    # Lazy import inside this branch so the env-override path never
    # depends on testcontainers being importable. (It is, in the
    # current uv.lock, but the gate keeps the dependency surface
    # localised to the testcontainers-using code path.)
    try:
        from testcontainers.core.container import DockerContainer  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dev-deps not installed
        pytest.skip(f"testcontainers unavailable: {exc}")

    try:
        with _boot_vcsim_container(topology) as endpoint:
            yield endpoint
    except Exception as exc:
        pytest.skip(f"vcsim container failed to start ({type(exc).__name__}): {exc}")


def build_vcsim_http_client(endpoint: VcsimEndpoint) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` wired for vcsim's self-signed cert.

    vcsim's bundled cert isn't in the host CA bundle, so the client
    disables verification — production code never reaches this path.
    Timeouts match the connector's defaults so behaviour under load
    mirrors a real dispatch.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return httpx.AsyncClient(
        base_url=endpoint.base_url,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
        verify=ssl_ctx,
    )


def patch_vmware_connector_for_vcsim(
    connector: Any,
    endpoint: VcsimEndpoint,
) -> None:
    """Mutate *connector* in-place to dispatch against *endpoint* over TLS-skip.

    Three mutations:

    1. Replaces ``_session_loader`` with a closure returning vcsim's
       any-credentials pair — bypasses the default Vault read which
       isn't wired in the acceptance suite.
    2. Replaces ``_http_client`` so the per-target :class:`httpx.AsyncClient`
       pool builds with ``verify=False`` (vcsim's self-signed cert
       isn't trusted by the host CA bundle).
    3. Leaves ``_session_tokens`` untouched — the connector's own
       session-establishment flow (``POST /api/session``) runs against
       vcsim as designed; the simulator accepts any basic-auth pair
       and returns a synthetic token.

    The mutations are scoped to one connector instance; tests using
    this helper MUST run their own ``reset_dispatcher_caches()`` in
    teardown so a downstream test that resolves a fresh instance
    isn't poisoned by the patched one. Production code paths never
    touch ``_session_loader`` or ``_http_client`` directly.
    """
    import asyncio
    from typing import cast

    async def _vcsim_loader(_target: Any) -> dict[str, str]:
        return {"username": endpoint.username, "password": endpoint.password}

    connector._session_loader = _vcsim_loader

    # Per-target lock the patch uses — kept disjoint from the
    # connector's own ``_lock`` so concurrent acquisitions don't
    # deadlock if the connector's parent class already holds it.
    patch_lock = asyncio.Lock()
    base_url = endpoint.base_url

    async def _insecure_http_client(target: Any) -> httpx.AsyncClient:
        async with patch_lock:
            clients = cast(dict[str, httpx.AsyncClient], connector._clients)
            if target.name not in clients:
                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                clients[target.name] = httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
                    verify=ssl_ctx,
                    # Mirror production HttpConnector._http_client:
                    # vcsim's legacy ``/rest`` mount 301s missing
                    # trailing slashes, so the patched client must
                    # follow redirects too or the dispatch leg sees a
                    # spurious HTTPStatusError instead of vcsim data.
                    follow_redirects=True,
                )
            return clients[target.name]

    connector._http_client = _insecure_http_client  # type: ignore[method-assign]
