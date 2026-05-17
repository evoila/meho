# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration smoke test for :class:`Bind9Connector` against a real bind9 + SSH.

Boots a small Debian-bookworm container with ``bind9`` and
``openssh-server`` installed and exercises the connector's
:meth:`fingerprint` and :meth:`probe` end-to-end via the SSH adapter's
real ``asyncssh.connect`` path. The image is built from an inline
Dockerfile at fixture-setup time so the test does not depend on an
externally-curated bind9+SSH image; the build is cached by testcontainers
across runs (BuildKit hashes the file).

Skip conditions:

* Docker socket missing -- same heuristic the rest of the
  ``tests/integration/`` package uses; agent sandboxes without Docker
  skip, CI runners with Docker provisioned run.
* ``testcontainers`` Python package itself unimportable (extremely
  rare; covered for defensive completeness).

Once ``DOCKER_AVAILABLE`` is true, image build and container start
failures are **not** caught and converted to ``pytest.skip``: a broken
Dockerfile, a missing apt package on bookworm, or an entrypoint
regression must surface as a test failure in the CI integration job
rather than masquerade as a clean skip. Earlier versions wrapped
``image.build()`` / ``container.start()`` in ``except Exception:
pytest.skip(...)`` so the agent sandbox would not flag red on
transient Docker quirks; that swallowed real regressions, so the
swallow is gone.

The image build is bounded (~150 MiB after apt installs ``bind9 bind9-host
bind9utils openssh-server``); the container start is bounded by the
``apt install`` time, not the boot time, so the fixture is module-
scoped and amortised across the two tests in this module.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.connectors.bind9 import Bind9Connector

# ---------------------------------------------------------------------------
# Docker availability -- mirrors tests/integration/conftest.py heuristic
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


# ---------------------------------------------------------------------------
# Target stub -- minimal shape SshConnector reads from
# ---------------------------------------------------------------------------


@dataclass
class _Bind9Target:
    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]


# ---------------------------------------------------------------------------
# Inline Dockerfile -- Debian bookworm with bind9 + openssh-server
# ---------------------------------------------------------------------------


_DOCKERFILE: str = textwrap.dedent(
    """\
    FROM debian:bookworm-slim

    ENV DEBIAN_FRONTEND=noninteractive

    RUN apt-get update \\
     && apt-get install -y --no-install-recommends \\
          bind9 bind9-host bind9utils \\
          openssh-server \\
     && rm -rf /var/lib/apt/lists/*

    # Allow root SSH login with password for the test fixture only;
    # this image is built and torn down per CI run, never exposed
    # outside the testcontainers network.
    RUN mkdir -p /var/run/sshd \\
     && echo 'root:testpw' | chpasswd \\
     && sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
     && sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config

    # Wrapper that starts named in the background and then runs sshd
    # in the foreground so PID 1 stays alive.
    RUN printf '#!/bin/sh\\n/usr/sbin/named -u bind\\nexec /usr/sbin/sshd -D -e\\n' \\
            > /entrypoint.sh \\
     && chmod +x /entrypoint.sh

    EXPOSE 22
    CMD ["/entrypoint.sh"]
    """
)


# ---------------------------------------------------------------------------
# Module-scoped container fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bind9_container_target() -> Iterator[_Bind9Target]:
    """Build the bind9 image, start the container, yield a target stub.

    The image is built once per pytest invocation; testcontainers'
    BuildKit driver caches layers across runs so subsequent local
    iterations skip the apt install. Container is shut down on
    fixture teardown.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    # Local imports -- testcontainers transitively imports the docker
    # SDK which probes the socket on import. Keeping the imports
    # inside the fixture lets the module collect on a no-Docker
    # sandbox.
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.image import DockerImage
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError as exc:  # pragma: no cover -- testcontainers ships these in 4.x
        pytest.skip(f"testcontainers missing module: {exc}")

    with tempfile.TemporaryDirectory() as build_dir:
        dockerfile = Path(build_dir) / "Dockerfile"
        dockerfile.write_text(_DOCKERFILE)

        # ``MEHO_TEST_BIND9_TAG`` lets CI mirror the locally-built tag
        # to a registry / pin it across runs; default tag stays local-
        # only so a missing override does not race to publish.
        tag = os.environ.get("MEHO_TEST_BIND9_TAG", "meho-test-bind9:9.18-bookworm")

        # Build the image. ``DOCKER_AVAILABLE`` is already true here, so
        # any build failure is a real Dockerfile / package / index-fetch
        # regression and must surface as a test failure rather than a
        # skip. The pre-fix shape wrapped this in ``except Exception:
        # pytest.skip(...)`` and lost CI signal on broken images.
        image = DockerImage(path=build_dir, tag=tag)
        image.build()

        container = DockerContainer(tag).with_exposed_ports(22)
        container.start()
        try:
            # Wait for sshd to log readiness before tests connect.
            # ``wait_for_logs`` raises ``TimeoutError`` after 30 s of no
            # match; we let it propagate so a regression in the inline
            # entrypoint surfaces rather than silently skipping. The
            # container is torn down in the outer ``finally`` regardless.
            wait_for_logs(container, "Server listening on", timeout=30.0)
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(22))
            # named starts in the background just before sshd; give
            # it a moment for ``pgrep -x named`` to see the process.
            time.sleep(2.0)
            target = _Bind9Target(
                name="bind9-test",
                host=host,
                port=port,
                secret_ref={"username": "root", "password": "testpw"},  # NOSONAR -- container-local
            )
            yield target
        finally:
            container.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_against_real_bind9_returns_canonical_shape(
    bind9_container_target: _Bind9Target,
) -> None:
    """Live fingerprint against a real bind9 container parses version + OS."""
    connector = Bind9Connector()
    try:
        result = await connector.fingerprint(bind9_container_target)
    finally:
        await connector.aclose()

    assert result.vendor == "isc"
    assert result.product == "bind9"
    # Debian bookworm ships bind9 9.18.x; the regex must recover the
    # X.Y.Z triple regardless of the exact patch level.
    assert result.version is not None
    assert result.version.startswith("9.18.")
    assert result.reachable is True
    assert result.probe_method == "ssh: named -v"
    # ``/etc/os-release`` on bookworm carries ID=debian VERSION_ID="12".
    os_identifier = result.extras.get("os")
    assert os_identifier is not None
    assert "debian" in os_identifier.lower()


@pytest.mark.asyncio
async def test_probe_against_real_bind9_returns_ok(
    bind9_container_target: _Bind9Target,
) -> None:
    """Live probe traverses tcp -> ssh -> auth -> named -> checkconf -> ok."""
    connector = Bind9Connector()
    try:
        result = await connector.probe(bind9_container_target)
    finally:
        await connector.aclose()

    assert result.ok is True, f"probe returned not-ok: reason={result.reason!r}"
    assert result.reason is None
    assert result.latency_ms is not None and result.latency_ms >= 0.0
