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
import shlex
import tempfile
import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from meho_backplane.connectors.bind9 import Bind9Connector
from tests._ssh_vault_stub import stub_ssh_vault_secrets

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
    # A Vault KV-v2 path STRING (#2155). This lane has no Vault, so the
    # autouse ``_container_vault_secrets`` fixture routes
    # ``SshConnector._resolve_secret`` through an in-memory registry that
    # returns the container-local credentials.
    secret_ref: str
    # The SSH connection pool keys on ``target_cache_key`` (``(tenant_id,
    # id)``); a double missing either field raises ``AttributeError`` the
    # moment it reaches the pool. This testcontainers lane is the only one
    # that exercises the real SSH pool, so the missing fields surface only
    # here (the #1642 lesson — evoila/meho#1682).
    id: str = ""
    tenant_id: str = "00000000-0000-0000-0000-000000000000"

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"id-{self.name}"


#: The container-local root password. The bind9 container's sshd + sudo
#: both accept it; the write-op tests pass it explicitly to
#: ``_remote_bash_with_sudo`` and the auth path resolves it from the
#: stubbed Vault secret.
_CONTAINER_PASSWORD: str = "testpw"  # NOSONAR -- container-local, torn down per module

#: KV-v2 path the container target carries in ``secret_ref``; the autouse
#: fixture registers the resolved credential dict under it.
_CONTAINER_SECRET_PATH: str = "meho/testing/bind9/container"


@pytest.fixture(autouse=True)
def _container_vault_secrets() -> Iterator[None]:
    """Route ``SshConnector._resolve_secret`` to the container credentials.

    The container lane exercises the real SSH pool but has no Vault; the
    stub returns the same ``{username, password}`` the pre-#2155 embedded
    ``secret_ref`` dict carried.
    """
    with stub_ssh_vault_secrets(
        {_CONTAINER_SECRET_PATH: {"username": "root", "password": _CONTAINER_PASSWORD}}
    ):
        yield


# ---------------------------------------------------------------------------
# Inline Dockerfile -- Debian bookworm with bind9 + openssh-server
# ---------------------------------------------------------------------------


_DOCKERFILE: str = textwrap.dedent(
    """\
    FROM debian:bookworm-slim

    ENV DEBIAN_FRONTEND=noninteractive

    RUN apt-get update \\
     && apt-get install -y --no-install-recommends \\
          bind9 bind9-host bind9utils dnsutils \\
          openssh-server sudo python3 \\
     && rm -rf /var/lib/apt/lists/*

    # Allow root SSH login with password for the test fixture only;
    # this image is built and torn down per CI run, never exposed
    # outside the testcontainers network.
    RUN mkdir -p /var/run/sshd \\
     && echo 'root:testpw' | chpasswd \\
     && sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
     && sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config

    # Atomic-apply ops (record.add / record.remove and the two
    # rollback acceptance-criterion tests) route every remote write
    # through ``sudo -S -p '' bash -s``. The package above provides
    # ``sudo``; we ALSO grant root passwordless sudo here so the
    # ``_remote_bash_with_sudo`` helper's stdin password line does
    # not stall the pipeline. Root authenticates without password
    # already via the host's PAM rules, but ``sudo -S`` still reads
    # one line from stdin before exec'ing -- giving root NOPASSWD
    # makes the stdin password line a no-op rather than a required
    # consumer. This is fixture-only; production targets carry a
    # real sudo password on ``target.secret_ref``.
    RUN echo 'root ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers.d/99-meho-test \\
     && chmod 0440 /etc/sudoers.d/99-meho-test

    # Seed a test zone -- the read-op tests (bind9.zone.list /
    # bind9.zone.read / bind9.record.get) assert against this
    # fixture. The shape is intentionally minimal but covers each
    # supported record type (A / AAAA / CNAME / MX / TXT) so the
    # ``bind9.record.get`` test can exercise the type-default + the
    # type-explicit paths.
    RUN echo 'zone "evba.lab" { type master; file "/etc/bind/db.evba.lab"; };' \\
            >> /etc/bind/named.conf.local

    RUN printf '%s\\n' \\
            '$TTL 3600' \\
            '@ IN SOA ns1.evba.lab. admin.evba.lab. (' \\
            '    2026051801 3600 600 604800 86400 )' \\
            '@   IN NS ns1.evba.lab.' \\
            'ns1 IN A 10.5.50.1' \\
            'www IN A 10.5.50.2' \\
            'mail IN A 10.5.50.3' \\
            'mail IN AAAA 2001:db8::1' \\
            'alias IN CNAME ns1.evba.lab.' \\
            '@   IN MX 10 mail.evba.lab.' \\
            '@   IN TXT "v=spf1 a -all"' \\
            > /etc/bind/db.evba.lab \\
     && chown root:bind /etc/bind/db.evba.lab \\
     && chmod 644 /etc/bind/db.evba.lab

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

        from tests._strategies import wait_for_log_message
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
            # ``wait_for_log_message`` raises ``TimeoutError`` after 30 s
            # of no match; we let it propagate so a regression in the
            # inline entrypoint surfaces rather than silently skipping.
            # The container is torn down in the outer ``finally``
            # regardless.
            wait_for_log_message(container, "Server listening on", timeout=30.0)
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(22))
            # named starts in the background just before sshd; give
            # it a moment for ``pgrep -x named`` to see the process.
            time.sleep(2.0)
            target = _Bind9Target(
                name="bind9-test",
                host=host,
                port=port,
                secret_ref=_CONTAINER_SECRET_PATH,
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


# ---------------------------------------------------------------------------
# T2 read-op group -- bind9.zone.list / bind9.zone.read / bind9.record.get /
# bind9.config.show against the seeded zonefile.
#
# Each test exercises the bound-method handler shim directly against the
# live container (skipping the dispatcher's DB lookup + JSON Schema
# validation path -- that's covered by the unit suite). The shape under
# test here is "the handler talks SSH + parses real output correctly";
# the dispatch-shim + handler-resolution path is exercised in
# tests/test_connectors_bind9.py against the SQLite test DB.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zone_list_against_real_bind9_returns_seeded_zone(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.zone.list`` discovers the seeded ``evba.lab`` zone."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_zone_list(bind9_container_target, {})
    finally:
        await connector.aclose()

    rows = result["rows"]
    zone_names = {row["name"] for row in rows}
    assert "evba.lab" in zone_names, (
        f"expected ``evba.lab`` in zone.list result; got {zone_names!r}"
    )
    evba_row = next(r for r in rows if r["name"] == "evba.lab")
    assert evba_row["type"] == "master"
    assert evba_row["file"] == "/etc/bind/db.evba.lab"
    assert result["total"] == len(rows)


@pytest.mark.asyncio
async def test_zone_read_against_real_bind9_parses_seeded_records(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.zone.read evba.lab`` parses every seeded record type."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_zone_read(bind9_container_target, {"zone": "evba.lab"})
    finally:
        await connector.aclose()

    assert result["zone"] == "evba.lab"
    assert result["file"] == "/etc/bind/db.evba.lab"
    rows = result["rows"]
    # Each seeded record type must appear at least once.
    types = {row["type"] for row in rows}
    assert {"SOA", "NS", "A", "AAAA", "CNAME", "MX", "TXT"}.issubset(types), (
        f"expected each seeded record type in zone.read result; got types={types!r}"
    )
    # The www A record specifically -- the integration test's main
    # anchor for "parsing reaches the right rdata".
    www_row = next(
        (r for r in rows if r["name"] == "www.evba.lab." and r["type"] == "A"),
        None,
    )
    assert www_row is not None, f"missing www.evba.lab. A row in {rows!r}"
    assert www_row["rdata"] == "10.5.50.2"


@pytest.mark.asyncio
async def test_record_get_against_real_bind9_resolves_via_dig(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.record.get www.evba.lab`` resolves through the running daemon."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_record_get(bind9_container_target, {"fqdn": "www.evba.lab"})
    finally:
        await connector.aclose()

    assert result["type"] == "A"
    assert result["total"] >= 1
    rdatas = {row["rdata"] for row in result["rows"]}
    assert "10.5.50.2" in rdatas, f"expected 10.5.50.2 in {rdatas!r}"


@pytest.mark.asyncio
async def test_record_get_with_explicit_type_against_real_bind9(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.record.get mail.evba.lab --type AAAA`` returns the IPv6 row."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_record_get(
            bind9_container_target,
            {"fqdn": "mail.evba.lab", "type": "AAAA"},
        )
    finally:
        await connector.aclose()

    assert result["type"] == "AAAA"
    rdatas = {row["rdata"] for row in result["rows"]}
    assert "2001:db8::1" in rdatas, f"expected 2001:db8::1 in {rdatas!r}"


@pytest.mark.asyncio
async def test_config_show_against_real_bind9_reads_named_conf_local(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.config.show named.conf.local`` returns the file content."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_config_show(
            bind9_container_target, {"path": "named.conf.local"}
        )
    finally:
        await connector.aclose()

    assert result["file"] == "/etc/bind/named.conf.local"
    # The seed added ``zone "evba.lab" { ... };`` to named.conf.local;
    # the content read must contain that signature line.
    assert 'zone "evba.lab"' in result["content"]
    assert "/etc/bind/db.evba.lab" in result["content"]


@pytest.mark.asyncio
async def test_config_show_against_real_bind9_refuses_traversal_with_no_content(
    bind9_container_target: _Bind9Target,
) -> None:
    """A traversal path raises before any wire IO; no file content leaks."""
    from meho_backplane.connectors.bind9.ops_config import ConfigPathRejectedError

    connector = Bind9Connector()
    try:
        with pytest.raises(ConfigPathRejectedError):
            await connector.bind9_config_show(bind9_container_target, {"path": "../../etc/passwd"})
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# T3 record-write group -- bind9.record.add / bind9.record.remove + rollback
# proof against the seeded zone in the same container.
#
# The acceptance criterion that gates this Task is the "atomic-apply
# rollback proven": injecting a deliberately invalid staged zonefile
# AND a post-reload dig-verify failure must each leave ``/etc/bind/``
# byte-identical to the pre-op snapshot. The two
# ``test_atomic_apply_rollback_*`` tests below assert this via
# pre/post-op checksums computed on the container's bind tree.
# ---------------------------------------------------------------------------


async def _checksum_bind_tree(connector: Bind9Connector, target: _Bind9Target) -> str:
    """Return the SHA256-tree fingerprint of ``/etc/bind/`` on *target*.

    Normalises the SOA serial in every zonefile before hashing so a
    rollback that bumps the restored file's serial (the mechanism
    the atomic-apply primitive uses to defeat named's serial cache
    -- see ``_atomic.py`` rollback docstring) is treated as logically
    byte-identical to the pre-op snapshot. Every other byte (records,
    TTLs, directives, comments, whitespace) is hashed verbatim, so a
    rollback that loses or mutates any of those still trips the
    ``before == after`` assertion.

    The normalisation pattern mirrors the rollback's SOA-bump regex
    (``\bSOA\b mname rname ( serial ...``) and replaces the serial
    digit-run with the literal string ``SERIAL``. count=1 per file --
    a zonefile has exactly one SOA record by RFC 1035.

    Fail-closed on a non-zero exit: a failing probe (Python missing,
    /etc/bind missing, permissions changed mid-test) would otherwise
    collapse to an empty string, and the rollback assertion would
    silently pass on two empty strings -- the load-bearing acceptance
    criterion this test encodes. Surface the failure explicitly so a
    broken probe fails the test instead of false-positive-ing the
    rollback proof.
    """
    # Use shlex.quote to pass the python script as a single argv element
    # so backslash escapes survive intact through bash. Constructing the
    # one-liner inline with manual escape stacking corrupted the SOA
    # regex on bind9 9.18 / Python 3.11 (the container) -- the regex
    # engine saw literal backslashes instead of word-boundary metas.
    probe_script = (
        "import hashlib, pathlib, re\n"
        "soa_re = re.compile(\n"
        "    r'(\\bSOA\\b\\s+\\S+\\s+\\S+\\s*\\(?\\s*(?:;[^\\n]*\\n\\s*)*)(\\d+)',\n"
        "    re.IGNORECASE,\n"
        ")\n"
        "h = hashlib.sha256()\n"
        "for p in sorted(\n"
        "    p for p in pathlib.Path('/etc/bind').rglob('*') if p.is_file()\n"
        "):\n"
        "    data = p.read_bytes()\n"
        "    try:\n"
        "        text = data.decode('utf-8')\n"
        "        data = soa_re.sub(\n"
        "            lambda m: m.group(1) + 'SERIAL', text, count=1\n"
        "        ).encode('utf-8')\n"
        "    except UnicodeDecodeError:\n"
        "        pass\n"
        "    h.update(str(p).encode() + b'\\n')\n"
        "    h.update(data)\n"
        "print(h.hexdigest())\n"
    )
    cmd = f"python3 -c {shlex.quote(probe_script)}"
    proc = await connector._run_command(target, cmd, operator=None)
    exit_status = getattr(proc, "exit_status", 0)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    digest = stdout.strip() if isinstance(stdout, str) else ""
    assert exit_status == 0, (
        f"_checksum_bind_tree probe exited {exit_status}; stderr={getattr(proc, 'stderr', '')!r}"
    )
    assert digest, "_checksum_bind_tree returned empty digest -- rollback proof would false-pass"
    return digest


@pytest.mark.asyncio
async def test_record_add_against_real_bind9_resolves_post_apply(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.record.add api.evba.lab 10.5.50.99`` -> dig resolves the new IP."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_record_add(
            bind9_container_target,
            {
                "fqdn": "api.evba.lab",
                "ip": "10.5.50.99",
                "type": "A",
                "zone": "evba.lab",
            },
        )
        assert result["op_class"] == "write"
        assert result["zone"] == "evba.lab"
        assert "result_state_before" in result
        assert "result_state_after" in result
        # The before / after must differ -- the staged change is the
        # diff between them. ``api.evba.lab`` was absent before and
        # present after.
        assert "10.5.50.99" not in result["result_state_before"]
        assert "10.5.50.99" in result["result_state_after"]

        # Verify the change actually propagated to the running named.
        get_result = await connector.bind9_record_get(
            bind9_container_target,
            {"fqdn": "api.evba.lab"},
        )
        rdatas = {row["rdata"] for row in get_result["rows"]}
        assert "10.5.50.99" in rdatas
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_record_remove_against_real_bind9_clears_resolution(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.record.remove`` clears the A record; dig no longer resolves it."""
    connector = Bind9Connector()
    try:
        # First add a record so there's something to remove (idempotent
        # if the add test above already ran in the same module session).
        await connector.bind9_record_add(
            bind9_container_target,
            {
                "fqdn": "scratch.evba.lab",
                "ip": "10.5.50.50",
                "type": "A",
                "zone": "evba.lab",
            },
        )

        result = await connector.bind9_record_remove(
            bind9_container_target,
            {"fqdn": "scratch.evba.lab", "zone": "evba.lab"},
        )
        assert result["op_class"] == "write"

        # Confirm dig no longer resolves the record.
        get_result = await connector.bind9_record_get(
            bind9_container_target,
            {"fqdn": "scratch.evba.lab"},
        )
        assert get_result["total"] == 0
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_record_add_auto_resolves_zone_when_zone_omitted(
    bind9_container_target: _Bind9Target,
) -> None:
    """``--zone`` omitted -> handler picks ``evba.lab`` via longest-suffix match."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_record_add(
            bind9_container_target,
            {"fqdn": "auto-resolve.evba.lab", "ip": "10.5.50.77", "type": "A"},
        )
        assert result["zone"] == "evba.lab"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_record_add_rejects_unresolvable_fqdn_pre_staging(
    bind9_container_target: _Bind9Target,
) -> None:
    """An FQDN outside any served zone -> ZoneResolutionError, no staging."""
    from meho_backplane.connectors.bind9.ops_record import ZoneResolutionError

    connector = Bind9Connector()
    try:
        # Take a checksum first so we can assert no staging happened.
        before = await _checksum_bind_tree(connector, bind9_container_target)
        with pytest.raises(ZoneResolutionError):
            await connector.bind9_record_add(
                bind9_container_target,
                {"fqdn": "api.outside.example.com", "ip": "10.5.50.99", "type": "A"},
            )
        after = await _checksum_bind_tree(connector, bind9_container_target)
        # Acceptance criterion: ambiguous/unresolvable returns invalid_params
        # with NO staging performed. The checksum unchanged is the assertion.
        assert before == after, "unresolvable FQDN must not touch /etc/bind/"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_atomic_apply_rollback_on_dig_verify_failure_leaves_tree_unchanged(
    bind9_container_target: _Bind9Target,
) -> None:
    """Verify failure -> ``/etc/bind/`` byte-identical to pre-op snapshot.

    Acceptance criterion: "post-reload dig-verify failure leaves
    /etc/bind/ byte-identical to the pre-op snapshot (assert via
    checksum of the bind tree before/after)".

    We inject a deliberately-wrong verify by calling :func:`atomic_apply`
    directly with a ``verify_command`` that always fails. The
    rollback must restore the tree.
    """
    from meho_backplane.connectors.bind9._atomic import (
        AtomicApplyError,
        atomic_apply,
    )

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_container_target)
        with pytest.raises(AtomicApplyError) as exc_info:
            await atomic_apply(
                connector,
                bind9_container_target,
                sudo_password=_CONTAINER_PASSWORD,
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                # Stage a syntactically valid zonefile so checkzone passes
                # and reload succeeds. The verify predicate (below) is the
                # forced failure surface.
                staged_bytes=(
                    b"$TTL 3600\n"
                    b"@ IN SOA ns1.evba.lab. admin.evba.lab. "
                    b"(2026051899 3600 600 604800 86400)\n"
                    b"@ IN NS ns1.evba.lab.\n"
                    b"ns1 IN A 10.5.50.1\n"
                    b"rollback-canary IN A 10.5.50.123\n"
                ),
                # ``false`` always exits non-zero -- the verify step
                # detects "named loaded the change but it doesn't
                # resolve" and rolls back.
                verify_command="false",
            )
        assert exc_info.value.step == "verify"

        after = await _checksum_bind_tree(connector, bind9_container_target)
        assert before == after, (
            f"atomic-apply did NOT roll back cleanly on verify failure; "
            f"checksum before={before!r}, after={after!r}"
        )

        # Defence-in-depth: the canary IP must not resolve either.
        get_result = await connector.bind9_record_get(
            bind9_container_target,
            {"fqdn": "rollback-canary.evba.lab"},
        )
        assert get_result["total"] == 0, "rollback-canary record survived a verify-failure rollback"
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_atomic_apply_rollback_on_checkzone_failure_leaves_tree_unchanged(
    bind9_container_target: _Bind9Target,
) -> None:
    """Validation failure -> ``/etc/bind/`` byte-identical to pre-op snapshot.

    Acceptance criterion: "injecting a named-checkconf failure
    (malformed staged zonefile) leaves /etc/bind/ byte-identical to
    the pre-op snapshot (assert via checksum of the bind tree
    before/after)".

    The "named-checkconf failure" maps to the primitive's
    ``checkconf`` step (which runs ``named-checkzone`` against the
    staged file). We inject a syntactically broken zonefile.
    """
    from meho_backplane.connectors.bind9._atomic import (
        AtomicApplyError,
        atomic_apply,
    )

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_container_target)
        with pytest.raises(AtomicApplyError) as exc_info:
            await atomic_apply(
                connector,
                bind9_container_target,
                sudo_password=_CONTAINER_PASSWORD,
                audit_slice_path="/etc/bind/db.evba.lab",
                zone_name="evba.lab",
                # Garbage zonefile -- named-checkzone refuses to load
                # a file with no SOA and undefined directives.
                staged_bytes=b"this is not a valid zonefile\nblargh blargh\n",
                # Verify doesn't run; checkzone refuses first.
                verify_command="true",
            )
        assert exc_info.value.step == "checkconf"

        after = await _checksum_bind_tree(connector, bind9_container_target)
        assert before == after, (
            f"atomic-apply did NOT roll back cleanly on checkzone failure; "
            f"checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# T4 config-write group -- bind9.config.apply_file / apply_views / backup /
# reload against the seeded container. Acceptance criteria:
#
# * ``apply_views`` applies a valid multi-file tree; an invalid views file
#   rolls back to a byte-identical /etc/bind/ (checksum assertion).
# * ``apply_file`` applies a valid fragment; an invalid fragment rolls back
#   identically.
# * Both ``apply_*`` ops invoke T3's ``_atomic.py`` primitive (the
#   ``op_class=write`` envelope + ``result_state_*`` capture come from the
#   primitive directly -- a non-primitive implementation would not emit
#   them).
# * ``config.backup`` produces a restorable tar.gz and returns a backup
#   ID + listing.
# * ``config.reload`` returns a structured success/failure envelope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_reload_against_real_bind9_returns_ok(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.config.reload`` succeeds against the live named."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_config_reload(bind9_container_target, {})
        assert result["ok"] is True, f"reload failed: {result!r}"
        assert result["rndc_reload_exit"] == 0
        assert result["op_class"] == "write"
        # rndc status output -- the live named's snapshot before/after.
        # Carries the BIND version and the per-zone status; the exact
        # text varies by version but the word "server" / "version"
        # always shows up.
        assert (
            "version" in result["result_state_after"].lower()
            or "server" in result["result_state_after"].lower()
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_config_backup_against_real_bind9_creates_archive(
    bind9_container_target: _Bind9Target,
) -> None:
    """``bind9.config.backup`` creates a tar.gz under /var/backups/meho-bind9/."""
    connector = Bind9Connector()
    try:
        result = await connector.bind9_config_backup(bind9_container_target, {"tag": "ci-smoke"})
        assert result["op_class"] == "write"
        assert "ci-smoke" in result["backup_id"]
        assert result["path"].startswith("/var/backups/meho-bind9/")
        assert result["path"].endswith(".tar.gz")
        # The listing must contain at least this backup we just made.
        ids = {row["id"] for row in result["rows"]}
        assert result["backup_id"] in ids
        # Verify the artifact actually exists on disk. ``shlex.quote``
        # is defence-in-depth -- the backup path is currently composed
        # from a tag pattern + timestamp + hex suffix, none of which
        # carries shell metacharacters, but quoting keeps the test
        # robust if the path schema ever picks up a wider character
        # class (e.g. tag pattern relaxed in a future op).
        cmd = f"ls -1 {shlex.quote(result['path'])}"
        proc = await connector._run_command(bind9_container_target, cmd, operator=None)
        ls_stderr = getattr(proc, "stderr", "")
        assert proc.exit_status == 0, (
            f"backup file missing: ls exit={proc.exit_status} stderr={ls_stderr!r}"
        )
        # The state_after carries the backup ID (the audit row's "what
        # artifact this write produced" signal).
        assert result["state_after"] == result["backup_id"]
        # No state_before -- nothing in /etc/bind/ mutated.
        assert "state_before" not in result
        assert "result_state_before" not in result
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_config_apply_file_against_real_bind9_lands_and_reloads(
    bind9_container_target: _Bind9Target,
) -> None:
    """``apply_file`` writes a valid fragment; named-checkconf + reload pass."""
    connector = Bind9Connector()
    try:
        # Append a comment-only fragment via apply_file -- safe to
        # write because it's not parsed by any other include. The
        # primitive's validate (named-checkconf -p) must accept it.
        content = "// meho integration test fragment\n"
        result = await connector.bind9_config_apply_file(
            bind9_container_target,
            {"path": "named.conf.options", "content": content},
        )
        assert result["op_class"] == "write"
        assert result["file"] == "/etc/bind/named.conf.options"
        # state_after must equal the staged bytes (the primitive
        # captures it post-write).
        assert result["result_state_after"] == content
        # The before / after must differ -- the seeded options file
        # is not the comment-only version we just wrote.
        assert result["result_state_after"] != result["result_state_before"]
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_config_apply_file_rollback_on_invalid_fragment_leaves_tree_unchanged(
    bind9_container_target: _Bind9Target,
) -> None:
    """An invalid fragment -> rollback; /etc/bind/ byte-identical post-op."""
    from meho_backplane.connectors.bind9._atomic import AtomicApplyError

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_container_target)
        # Garbage fragment -- named-checkconf -p refuses to parse it.
        garbage = "this is { not valid bind9 config\n"
        with pytest.raises(AtomicApplyError) as exc_info:
            await connector.bind9_config_apply_file(
                bind9_container_target,
                {"path": "named.conf.local", "content": garbage},
            )
        # Failure must happen at the checkconf step -- the primitive's
        # validate ran and refused.
        assert exc_info.value.step == "checkconf"

        after = await _checksum_bind_tree(connector, bind9_container_target)
        assert before == after, (
            f"apply_file did NOT roll back cleanly on invalid fragment; "
            f"checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_config_apply_views_against_real_bind9_lands_multi_file_tree(
    bind9_container_target: _Bind9Target,
) -> None:
    """``apply_views`` writes a multi-file tree; the tree shows up on disk."""
    connector = Bind9Connector()
    try:
        # Stage a new fragment + the existing options file (keep that
        # one identical so we don't break the live config). The
        # archive overlays the live tree -- the fragment lands, the
        # options stay byte-identical.
        new_fragment = "// apply_views integration smoke fragment\n"
        result = await connector.bind9_config_apply_views(
            bind9_container_target,
            {
                "files": {
                    "named.conf.smoke": new_fragment,
                },
            },
        )
        assert result["op_class"] == "write"
        assert "/etc/bind/named.conf.smoke" in result["files"]
        # The fragment we deposited must be on disk -- cat it back.
        cat_proc = await connector._run_command(
            bind9_container_target, "cat /etc/bind/named.conf.smoke"
        )
        assert cat_proc.exit_status == 0
        assert "smoke fragment" in (cat_proc.stdout or "")
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_config_apply_views_rollback_on_invalid_tree_leaves_tree_unchanged(
    bind9_container_target: _Bind9Target,
) -> None:
    """An invalid views file -> rollback; /etc/bind/ byte-identical post-op.

    The load-bearing T4 acceptance criterion: ``apply_views`` reuses
    the primitive's snapshot-rollback contract for multi-file trees.
    A bad fragment in the staged archive must NOT leave the bind tree
    in a half-applied state -- the primitive's
    ``find $BIND_ROOT -mindepth 1 -delete`` clear-then-extract sequence
    must clear the orphan fragment introduced by the failed stage.
    """
    from meho_backplane.connectors.bind9._atomic import AtomicApplyError

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_container_target)
        # The staged archive overlays /etc/bind/named.conf.local with
        # garbage -- the existing valid file is byte-replaced. The
        # primitive's validate (named-checkconf -p) must refuse it.
        with pytest.raises(AtomicApplyError) as exc_info:
            await connector.bind9_config_apply_views(
                bind9_container_target,
                {
                    "files": {
                        # Overwrite the live named.conf.local with garbage --
                        # bind9 must refuse to parse this on validate.
                        "named.conf.local": "this is { not valid bind9 config\n",
                    },
                },
            )
        assert exc_info.value.step == "checkconf"

        after = await _checksum_bind_tree(connector, bind9_container_target)
        assert before == after, (
            f"apply_views did NOT roll back cleanly on invalid tree; "
            f"checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()
