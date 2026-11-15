# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration smoke test for :class:`Bind9Connector` against BIND 9.20.

The sibling module ``test_connectors_bind9_container.py`` runs the
connector against BIND 9.18 (Debian bookworm). This module runs the same
image recipe on Debian trixie, which ships BIND 9.20, so both ends of the
declared range ``>=9.18,<9.21`` are tested in CI.

Like the sibling, it skips only when no Docker socket exists. A broken
image build or container start fails the test.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.connectors.bind9 import Bind9Connector
from tests.integration.test_connectors_bind9_container import (
    _CONTAINER_SECRET_PATH,
    _DOCKERFILE,
    DOCKER_AVAILABLE,
    SKIP_REASON,
    _Bind9Target,
    _container_vault_secrets,  # noqa: F401 -- autouse fixture, re-exported to this module
)

_TRIXIE_DOCKERFILE: str = _DOCKERFILE.replace("debian:bookworm-slim", "debian:trixie-slim")


@pytest.fixture(scope="module")
def bind9_920_target() -> Iterator[_Bind9Target]:
    """Build the trixie image, start the container, yield a target stub."""
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.image import DockerImage

    from tests._strategies import wait_for_log_message

    assert _TRIXIE_DOCKERFILE != _DOCKERFILE, "the base image line was not replaced"

    with tempfile.TemporaryDirectory() as build_dir:
        (Path(build_dir) / "Dockerfile").write_text(_TRIXIE_DOCKERFILE)
        tag = os.environ.get("MEHO_TEST_BIND9_920_TAG", "meho-test-bind9:9.20-trixie")
        DockerImage(path=build_dir, tag=tag).build()

        container = DockerContainer(tag).with_exposed_ports(22)
        container.start()
        try:
            wait_for_log_message(container, "Server listening on", timeout=30.0)
            # named starts just before sshd; give it a moment to be seen.
            import time

            time.sleep(2.0)
            yield _Bind9Target(
                name="bind9-920-test",
                host=container.get_container_host_ip(),
                port=int(container.get_exposed_port(22)),
                secret_ref=_CONTAINER_SECRET_PATH,
            )
        finally:
            container.stop()


@pytest.mark.asyncio
async def test_fingerprint_against_bind_920(bind9_920_target: _Bind9Target) -> None:
    """The version regex reads the 9.20 banner, and the version is in range."""
    connector = Bind9Connector()
    try:
        result = await connector.fingerprint(bind9_920_target)
    finally:
        await connector.aclose()

    assert result.product == "bind9"
    assert result.version is not None
    assert result.version.startswith("9.20.")
    assert result.reachable is True


@pytest.mark.asyncio
async def test_probe_against_bind_920_returns_ok(bind9_920_target: _Bind9Target) -> None:
    """tcp, ssh, auth, named and named-checkconf all pass on 9.20."""
    connector = Bind9Connector()
    try:
        result = await connector.probe(bind9_920_target)
    finally:
        await connector.aclose()

    assert result.ok is True, f"probe returned not-ok: reason={result.reason!r}"


@pytest.mark.asyncio
async def test_zone_list_and_record_get_against_bind_920(bind9_920_target: _Bind9Target) -> None:
    """The read operations work on 9.20: zone list and a live record lookup."""
    connector = Bind9Connector()
    try:
        zones = await connector.bind9_zone_list(bind9_920_target, {})
        names = [row["name"] for row in zones["rows"]]
        assert names, "zone.list returned no zones"
        # The image seeds one forward zone with a ``www`` A record.
        zone = next((n for n in names if not n.rstrip(".").endswith(".arpa")), names[0])
        zone = zone.rstrip(".")
        record = await connector.bind9_record_get(bind9_920_target, {"fqdn": f"www.{zone}"})
    finally:
        await connector.aclose()

    assert record["rows"], f"no record rows for www.{zone}"
    assert any(row["rdata"] for row in record["rows"])
