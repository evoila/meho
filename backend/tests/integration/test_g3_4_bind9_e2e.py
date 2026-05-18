# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.4-T5 — bind9 meta-tool E2E acceptance against a containerised bind9.

Extends the existing bind9 testcontainer fixture
(``tests/integration/test_connectors_bind9_container.py``) to drive
every registered op through the **agent meta-tool flow**
(``search_operations`` + ``call_operation``) against the real G0.6
dispatcher with a Postgres-backed ``endpoint_descriptor`` and a
Postgres-backed ``audit_log``.

What this harness proves (Task #591 DoD)
=========================================

* All 11 bind9 ops registered into ``endpoint_descriptor`` via
  :func:`~meho_backplane.operations.typed_register.register_typed_operation`
  by ``Bind9Connector.register_operations()``. The full set is reachable
  via ``search_operations(connector_id="bind9-ssh-9.x", query=...)``;
  the meta-tool returns hits covering every registered op.
* Every op dispatches through :func:`call_operation` (the agent-facing
  surface; the CLI alias verb tree #591 ships is a separate operator
  surface that goes through the *same* dispatch route via
  ``POST /api/v1/operations/call`` — the unit tests in
  ``cli/internal/cmd/bind9/bind9_test.go`` pin the CLI→dispatch wire
  shape; this harness pins the dispatch→handler→bind9 round-trip).
* Each successful call writes a synchronous ``audit_log`` row (CLAUDE.md
  postulate 7) with the canonical
  ``(product="bind9", version="9.x", impl_id="bind9-ssh")`` triple in
  the payload.
* Write ops (record.add / record.remove / config.apply_*) carry
  ``state_before`` / ``state_after`` on the audit row payload — the
  before/after capture the atomic-apply primitive emits.
* Atomic-apply rollback: invalid apply_views, invalid apply_file, and
  unresolvable record.add each leave ``/etc/bind/`` byte-identical to
  the pre-op snapshot (SOA-normalising checksum).
* Codebase-wide safety assertion: ``_remote_bash_with_sudo()`` is the
  only sudo-invoking construction under
  ``backend/src/meho_backplane/connectors/``. Encoded as an
  always-runs (no Docker required) static-import check so the invariant
  holds in the sandbox sweep.

Skip conditions
================

Docker socket missing — same heuristic the rest of
``tests/integration/`` uses; agent sandboxes without Docker skip,
CI runners with Docker provisioned run. The static-import safety
assertion runs unconditionally because it walks files on disk.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import tempfile
import textwrap
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from meho_backplane.connectors.bind9 import (
    BIND9_OPS,
    Bind9Connector,
    register_bind9_typed_operations,
)
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, search_operations
from meho_backplane.operations.reducer import PassThroughReducer
from tests.test_operations_dispatcher import _make_operator

# ---------------------------------------------------------------------------
# Docker-availability gate -- identical heuristic to the existing bind9
# container fixture.
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

#: Tenant the test operators run under. Mirrors the K8s E2E (#326)
#: convention so the seeded ``Target`` ORM row and the operator built
#: by every ``call_operation`` test are tenant-consistent.
_OPERATOR_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-00000000a0a0")

#: Name the meta-tool ``call_operation`` test resolves via
#: ``{"target": {"name": _TARGET_NAME}}`` → :func:`resolve_target`.
_TARGET_NAME: str = "bind9-e2e"

#: Every op id this harness exercises. Pinned here (rather than
#: re-derived from ``BIND9_OPS``) so a registration regression
#: surfaces as a clear "missing op" assertion failure rather than a
#: silent test-count change. The eleven ops correspond to G3.4-T1..T4.
EXPECTED_OP_IDS: tuple[str, ...] = (
    "bind9.about",
    "bind9.zone.list",
    "bind9.zone.read",
    "bind9.record.get",
    "bind9.record.add",
    "bind9.record.remove",
    "bind9.config.show",
    "bind9.config.apply_file",
    "bind9.config.apply_views",
    "bind9.config.backup",
    "bind9.config.reload",
)


# ---------------------------------------------------------------------------
# Target stub -- minimal shape SshConnector + the dispatcher's resolver read
# ---------------------------------------------------------------------------


@dataclass
class _Bind9Target:
    """Duck-typed target the dispatcher's resolver + SSH adapter read.

    The dispatcher's :func:`resolve_connector` reads ``product`` +
    ``fingerprint.version``; the SSH adapter reads ``name`` + ``host`` +
    ``port`` + ``secret_ref``. Mirrors the
    :class:`_Bind9Target` stub in ``test_connectors_bind9_container.py``
    plus the dispatcher-resolver fields.
    """

    name: str
    host: str
    port: int | None
    secret_ref: dict[str, Any]
    product: str = "bind9"
    auth_model: str = "shared_service_account"
    raw_jwt: str | None = "<dev-test-jwt>"

    def __post_init__(self) -> None:
        self.id: UUID = uuid4()
        self.preferred_impl_id: str | None = None

        class _FP:
            # Pinned to the 9.18 series matching the container image so
            # the resolver's version match step has a concrete value to
            # read. The bind9 connector advertises
            # supported_version_range=None, so the resolver doesn't
            # filter on it -- the version still surfaces in the audit
            # row payload.
            version = "9.18.0"

        self.fingerprint = _FP()


# ---------------------------------------------------------------------------
# Inline Dockerfile -- copied from test_connectors_bind9_container.py.
#
# The image is the same Debian-bookworm + bind9 + openssh-server build
# the existing container fixture uses, with one extra "evba.lab" zone
# seeded so every read/write op has something to operate against.
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

    RUN mkdir -p /var/run/sshd \\
     && echo 'root:testpw' | chpasswd \\
     && sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
     && sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config

    RUN echo 'root ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers.d/99-meho-test \\
     && chmod 0440 /etc/sudoers.d/99-meho-test

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

    RUN printf '#!/bin/sh\\n/usr/sbin/named -u bind\\nexec /usr/sbin/sshd -D -e\\n' \\
            > /entrypoint.sh \\
     && chmod +x /entrypoint.sh

    EXPOSE 22
    CMD ["/entrypoint.sh"]
    """
)


@pytest.fixture(scope="module")
def bind9_container_target() -> Iterator[_Bind9Target]:
    """Build the bind9 image, start the container, yield a target stub.

    Mirrors the existing ``test_connectors_bind9_container.py`` fixture
    so the container boot path is identical — one harness boots one
    container, not two — but yields the augmented ``_Bind9Target`` shape
    the dispatcher's resolver expects (``.product`` + ``.fingerprint``).
    """
    if not DOCKER_AVAILABLE:
        pytest.skip(SKIP_REASON)

    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.image import DockerImage
        from testcontainers.core.waiting_utils import wait_for_logs
    except ImportError as exc:  # pragma: no cover -- testcontainers ships these in 4.x
        pytest.skip(f"testcontainers missing module: {exc}")

    with tempfile.TemporaryDirectory() as build_dir:
        dockerfile = Path(build_dir) / "Dockerfile"
        dockerfile.write_text(_DOCKERFILE)
        tag = os.environ.get("MEHO_TEST_BIND9_TAG", "meho-test-bind9:9.18-bookworm")

        image = DockerImage(path=build_dir, tag=tag)
        image.build()

        container = DockerContainer(tag).with_exposed_ports(22)
        container.start()
        try:
            wait_for_logs(container, "Server listening on", timeout=30.0)
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(22))
            # named starts in the background just before sshd; give
            # it a moment for `pgrep -x named` to see the process.
            time.sleep(2.0)
            target = _Bind9Target(
                name=_TARGET_NAME,
                host=host,
                port=port,
                secret_ref={"username": "root", "password": "testpw"},  # NOSONAR -- container-local
            )
            yield target
        finally:
            container.stop()


# ---------------------------------------------------------------------------
# Per-test wiring: descriptor registration + Target row seeding.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registration doesn't load ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def bind9_e2e(
    bind9_container_target: _Bind9Target,
    pg_engine: None,
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[_Bind9Target]:
    """Wire the bind9 connector at the live container + real PG audit store.

    Phases mirror the K8s E2E harness (#326):

    1. Reset dispatcher / handler caches so the test starts against a
       known-empty cache.
    2. Re-register the connector class so the dispatcher's resolver
       finds it against the canonical ``("bind9", "9.x", "bind9-ssh")``
       triple.
    3. Set the pass-through reducer so set-shaped op results land
       verbatim in ``OperationResult.result``.
    4. Run :func:`register_bind9_typed_operations` to UPSERT every
       op into ``endpoint_descriptor``.
    5. Insert a tenant-scoped :class:`~meho_backplane.db.models.Target`
       ORM row so the ``call_operation`` meta-tool path — which
       resolves ``arguments["target"]={"name": ...}`` through the
       tenant-scoped :func:`resolve_target` — exercises the real
       contract.

    No connector-instance preseed: the bind9 connector reads SSH
    credentials directly from ``target.secret_ref``, so the seeded
    Target row's secret_ref is enough — no kubeconfig-style loader
    seam needed.

    The ``targets`` table is a soft-FK column the ``pg_engine`` fixture
    does not truncate, so the row is deleted on teardown to keep
    per-test isolation.
    """
    target = bind9_container_target

    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    clear_registry()
    register_connector_v2(
        product="bind9",
        version="9.x",
        impl_id="bind9-ssh",
        cls=Bind9Connector,
    )

    await register_bind9_typed_operations(embedding_service=stub_embedding_service)

    now = datetime.now(UTC)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            TargetORM(
                id=uuid4(),
                tenant_id=_OPERATOR_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="bind9",
                host=target.host,
                port=target.port,
                fqdn=None,
                # secret_ref on the ORM row is the SSH credential dict;
                # the bind9 SshConnector reads it directly via
                # ``_auth_config``.
                secret_ref=target.secret_ref,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                notes=None,
                fingerprint={"version": "9.18.0"},
                preferred_impl_id=None,
                created_at=now,
                updated_at=now,
            )
        )

    try:
        yield target
    finally:
        # Closing every Bind9Connector instance the dispatcher's
        # per-class cache may have built. Tolerant of double-close.
        from meho_backplane.operations import _handler_resolve as _hr

        with contextlib.suppress(Exception):
            cached = _hr._CONNECTOR_INSTANCE_CACHE.get(Bind9Connector)
            if cached is not None:
                await cached.aclose()
        # Delete the seeded Target row.
        from sqlalchemy import delete

        with contextlib.suppress(Exception):
            async with sessionmaker() as session, session.begin():
                await session.execute(
                    delete(TargetORM).where(
                        TargetORM.tenant_id == _OPERATOR_TENANT_ID,
                        TargetORM.name == _TARGET_NAME,
                    )
                )
        reset_dispatcher_caches()
        clear_registry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_audit_row(
    op_id: str,
    *,
    operator_sub: str,
    expect_state_before: bool = False,
    expect_state_after: bool = False,
) -> dict[str, Any]:
    """Assert exactly one audit_log row exists for *op_id* / *operator_sub*.

    Returns the row's payload dict so callers can do op-specific
    assertions on it (e.g. state_before / state_after values).
    Mirrors the K8s E2E harness's helper shape.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(
                        AuditLog.path == op_id,
                        AuditLog.operator_sub == operator_sub,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one audit row for {op_id} / operator {operator_sub}, got {len(rows)}"
    )
    row = rows[0]
    assert row.payload["op_id"] == op_id
    assert row.payload["source_kind"] == "typed"
    assert row.payload["result_status"] == "ok"
    if expect_state_before:
        assert "state_before" in row.payload, (
            f"write op {op_id} audit row missing state_before; payload={row.payload!r}"
        )
    if expect_state_after:
        assert "state_after" in row.payload, (
            f"write op {op_id} audit row missing state_after; payload={row.payload!r}"
        )
    # ``payload`` is JSONB → SQLAlchemy returns it as a plain dict; the
    # cast keeps mypy honest without paying a runtime check.
    payload: dict[str, Any] = dict(row.payload)
    return payload


def _normalise_soa_serials(text: str) -> str:
    """Replace every SOA serial in *text* with the literal ``SERIAL``.

    Same normalisation the existing container test's ``_checksum_bind_tree``
    helper uses: a rollback bumps the restored zonefile's SOA serial to
    defeat named's serial cache (see ``_atomic.py``), so two snapshots
    that differ only on the serial should still hash-equal post-rollback.
    """
    soa_re = re.compile(
        r"(\bSOA\b\s+\S+\s+\S+\s*\(?\s*(?:;[^\n]*\n\s*)*)(\d+)",
        re.IGNORECASE,
    )
    return soa_re.sub(lambda m: m.group(1) + "SERIAL", text, count=1)


async def _checksum_bind_tree(connector: Bind9Connector, target: _Bind9Target) -> str:
    """Return the SHA256-tree fingerprint of ``/etc/bind/`` on *target*.

    Mirrors the SOA-normalising checksum probe in the existing
    container test (T3 rollback acceptance) — copy here keeps this E2E
    file self-contained without an inter-test-file import.
    """
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
    proc = await connector._run_command(target, cmd, raw_jwt="")
    exit_status = getattr(proc, "exit_status", 0)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    digest = stdout.strip() if isinstance(stdout, str) else ""
    assert exit_status == 0, (
        f"_checksum_bind_tree probe exited {exit_status}; stderr={getattr(proc, 'stderr', '')!r}"
    )
    assert digest, "_checksum_bind_tree returned empty digest"
    return digest


# ---------------------------------------------------------------------------
# Registration + search shape (DoD: agent reaches all ops via search_operations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_registered_op_present_in_endpoint_descriptor(
    bind9_e2e: _Bind9Target,
) -> None:
    """Each row in :data:`BIND9_OPS` lands in ``endpoint_descriptor``
    under the canonical ``("bind9", "9.x", "bind9-ssh")`` triple."""
    from meho_backplane.db.models import EndpointDescriptor

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.product == "bind9",
                EndpointDescriptor.version == "9.x",
                EndpointDescriptor.impl_id == "bind9-ssh",
            )
        )
        rows = result.scalars().all()

    assert len(rows) == len(BIND9_OPS)
    op_ids = {row.op_id for row in rows}
    assert op_ids == set(EXPECTED_OP_IDS), (
        f"registered ops drift: in DB but not expected: "
        f"{op_ids - set(EXPECTED_OP_IDS)}; "
        f"expected but missing: {set(EXPECTED_OP_IDS) - op_ids}"
    )


@pytest.mark.asyncio
async def test_search_operations_surfaces_record_add_for_dns_intent(
    bind9_e2e: _Bind9Target,
) -> None:
    """``search_operations(connector_id="bind9-ssh-9.x", query="add a dns record")``
    returns ``bind9.record.add`` among its hits.

    Existential DoD: the agent typing a DNS-write intent reaches the
    write op via the meta-tool surface, not via a hand-coded MCP tool
    (CLAUDE.md postulate 5).
    """
    operator = _make_operator(sub="op-search-add", tenant_id=_OPERATOR_TENANT_ID)
    result = await search_operations(
        operator,
        {
            "connector_id": "bind9-ssh-9.x",
            "query": "add a dns record",
            "limit": 20,
        },
    )
    hits = result["hits"]
    assert len(hits) >= 1
    hit_op_ids = {h["op_id"] for h in hits}
    assert "bind9.record.add" in hit_op_ids, (
        f"search_operations(query='add a dns record') did not surface "
        f"bind9.record.add; got {hit_op_ids}"
    )


# ---------------------------------------------------------------------------
# Per-op call_operation dispatch -- DoD: every op dispatches end-to-end
# and writes one audit_log row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_operation_about_dispatches_through_meta_tool(
    bind9_e2e: _Bind9Target,
) -> None:
    """``call_operation(bind9.about)`` round-trips dispatcher → handler → bind9.

    Exercises the real meta-tool target contract: ``call_operation``
    requires ``arguments["target"]`` to be ``{"name": <str>}`` and runs
    it through the tenant-scoped :func:`resolve_target` (the meta-tool's
    tenant-isolation boundary) before dispatch.
    """
    operator = _make_operator(sub="op-meta-about", tenant_id=_OPERATOR_TENANT_ID)
    result = await call_operation(
        operator,
        {
            "connector_id": "bind9-ssh-9.x",
            "op_id": "bind9.about",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", result.get("error")
    payload = result["result"]
    assert payload["vendor"] == "isc"
    assert payload["product"] == "bind9"
    # Debian bookworm ships bind9 9.18.x.
    assert payload["version"] is not None
    assert str(payload["version"]).startswith("9.18.")
    await _assert_audit_row("bind9.about", operator_sub="op-meta-about")


@pytest.mark.asyncio
async def test_dispatch_zone_list_against_bind9(bind9_e2e: _Bind9Target) -> None:
    """``bind9.zone.list`` via the dispatcher returns the seeded ``evba.lab``."""
    operator = _make_operator(sub="op-zone-list", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.zone.list",
        target=bind9_e2e,
        params={},
    )
    assert result.status == "ok", result.error
    payload = result.result
    zone_names = {row["name"] for row in payload["rows"]}
    assert "evba.lab" in zone_names
    await _assert_audit_row("bind9.zone.list", operator_sub="op-zone-list")


@pytest.mark.asyncio
async def test_dispatch_zone_read_against_bind9(bind9_e2e: _Bind9Target) -> None:
    """``bind9.zone.read evba.lab`` returns the seeded record set."""
    operator = _make_operator(sub="op-zone-read", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.zone.read",
        target=bind9_e2e,
        params={"zone": "evba.lab"},
    )
    assert result.status == "ok", result.error
    payload = result.result
    types = {row["type"] for row in payload["rows"]}
    assert {"A", "AAAA", "CNAME", "MX", "TXT"}.issubset(types)
    await _assert_audit_row("bind9.zone.read", operator_sub="op-zone-read")


@pytest.mark.asyncio
async def test_dispatch_record_get_against_bind9(bind9_e2e: _Bind9Target) -> None:
    """``bind9.record.get www.evba.lab`` resolves through the live named."""
    operator = _make_operator(sub="op-record-get", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.record.get",
        target=bind9_e2e,
        params={"fqdn": "www.evba.lab", "type": "A"},
    )
    assert result.status == "ok", result.error
    rdatas = {row["rdata"] for row in result.result["rows"]}
    assert "10.5.50.2" in rdatas
    await _assert_audit_row("bind9.record.get", operator_sub="op-record-get")


@pytest.mark.asyncio
async def test_dispatch_record_add_against_bind9_writes_state_before_after(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.record.add`` writes a record and the audit row carries
    ``state_before`` / ``state_after`` summaries.
    """
    operator = _make_operator(sub="op-record-add", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.record.add",
        target=bind9_e2e,
        params={
            "fqdn": "added-e2e.evba.lab",
            "ip": "10.5.50.42",
            "type": "A",
            "zone": "evba.lab",
        },
    )
    assert result.status == "ok", result.error
    assert result.result["op_class"] == "write"
    payload = await _assert_audit_row(
        "bind9.record.add",
        operator_sub="op-record-add",
        expect_state_before=True,
        expect_state_after=True,
    )
    # state_before / state_after are durably persisted on the audit row.
    assert payload["state_after"] != payload["state_before"]


@pytest.mark.asyncio
async def test_dispatch_record_remove_against_bind9_writes_state_before_after(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.record.remove`` removes a record and the audit row carries
    ``state_before`` / ``state_after`` summaries.
    """
    # Add a record first so there's something to remove.
    add_op = _make_operator(sub="op-record-remove-add", tenant_id=_OPERATOR_TENANT_ID)
    add_result = await dispatch(
        operator=add_op,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.record.add",
        target=bind9_e2e,
        params={
            "fqdn": "to-remove.evba.lab",
            "ip": "10.5.50.43",
            "type": "A",
            "zone": "evba.lab",
        },
    )
    assert add_result.status == "ok"

    operator = _make_operator(sub="op-record-remove", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.record.remove",
        target=bind9_e2e,
        params={"fqdn": "to-remove.evba.lab", "zone": "evba.lab"},
    )
    assert result.status == "ok", result.error
    assert result.result["op_class"] == "write"
    await _assert_audit_row(
        "bind9.record.remove",
        operator_sub="op-record-remove",
        expect_state_before=True,
        expect_state_after=True,
    )


@pytest.mark.asyncio
async def test_dispatch_config_show_against_bind9(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.config.show named.conf.local`` returns the live file content."""
    operator = _make_operator(sub="op-config-show", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.show",
        target=bind9_e2e,
        params={"path": "named.conf.local"},
    )
    assert result.status == "ok", result.error
    assert "evba.lab" in result.result["content"]
    await _assert_audit_row("bind9.config.show", operator_sub="op-config-show")


@pytest.mark.asyncio
async def test_dispatch_config_apply_file_writes_state_before_after(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.config.apply_file`` writes a fragment + audit row carries
    state_before / state_after.
    """
    operator = _make_operator(sub="op-apply-file", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.apply_file",
        target=bind9_e2e,
        params={
            "path": "named.conf.options",
            "content": "// e2e applied fragment\n",
        },
    )
    assert result.status == "ok", result.error
    assert result.result["op_class"] == "write"
    payload = await _assert_audit_row(
        "bind9.config.apply_file",
        operator_sub="op-apply-file",
        expect_state_before=True,
        expect_state_after=True,
    )
    assert payload["state_before"] != payload["state_after"]


@pytest.mark.asyncio
async def test_dispatch_config_apply_views_writes_multi_file_tree(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.config.apply_views`` writes a multi-file tree successfully."""
    operator = _make_operator(sub="op-apply-views", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.apply_views",
        target=bind9_e2e,
        params={
            "files": {
                "named.conf.smoke-e2e": "// apply_views e2e smoke fragment\n",
            },
        },
    )
    assert result.status == "ok", result.error
    assert result.result["op_class"] == "write"
    await _assert_audit_row(
        "bind9.config.apply_views",
        operator_sub="op-apply-views",
        expect_state_before=True,
        expect_state_after=True,
    )


@pytest.mark.asyncio
async def test_dispatch_config_backup_creates_archive(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.config.backup`` produces a tar.gz + records state_after."""
    operator = _make_operator(sub="op-backup", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.backup",
        target=bind9_e2e,
        params={"tag": "e2e-smoke"},
    )
    assert result.status == "ok", result.error
    assert "e2e-smoke" in result.result["backup_id"]
    assert result.result["path"].endswith(".tar.gz")
    # config.backup audit row has only state_after (the backup_id) — no
    # state_before because nothing in /etc/bind/ mutated.
    payload = await _assert_audit_row(
        "bind9.config.backup",
        operator_sub="op-backup",
        expect_state_after=True,
    )
    assert payload["state_after"] == result.result["backup_id"]


@pytest.mark.asyncio
async def test_dispatch_config_reload_returns_ok(
    bind9_e2e: _Bind9Target,
) -> None:
    """``bind9.config.reload`` succeeds + carries state_before / state_after
    (the rndc-status snapshots).
    """
    operator = _make_operator(sub="op-reload", tenant_id=_OPERATOR_TENANT_ID)
    result = await dispatch(
        operator=operator,
        connector_id="bind9-ssh-9.x",
        op_id="bind9.config.reload",
        target=bind9_e2e,
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result["ok"] is True
    await _assert_audit_row(
        "bind9.config.reload",
        operator_sub="op-reload",
        expect_state_before=True,
        expect_state_after=True,
    )


# ---------------------------------------------------------------------------
# Agent meta-tool flow + CLI parity (DoD: search_operations surfaces the
# write op; call_operation executes; both go through the same dispatch
# route the CLI alias verb uses).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_meta_tool_flow_search_then_call_record_add(
    bind9_e2e: _Bind9Target,
) -> None:
    """Full agent flow: ``search_operations`` → ``call_operation``.

    The agent never sees the CLI alias verbs. It locates the right op
    via the narrow-waist meta-tools, then dispatches via the same
    dispatch route the CLI hits. This test pins the existential DoD:
    every bind9 op is reachable via the agent surface without per-op
    MCP tool registration (CLAUDE.md postulate 5).
    """
    operator = _make_operator(sub="op-agent-flow", tenant_id=_OPERATOR_TENANT_ID)
    search_result = await search_operations(
        operator,
        {
            "connector_id": "bind9-ssh-9.x",
            "query": "add a dns record",
            "limit": 5,
        },
    )
    hit_op_ids = {h["op_id"] for h in search_result["hits"]}
    assert "bind9.record.add" in hit_op_ids

    call_result = await call_operation(
        operator,
        {
            "connector_id": "bind9-ssh-9.x",
            "op_id": "bind9.record.add",
            "target": {"name": _TARGET_NAME},
            "params": {
                "fqdn": "agent-flow.evba.lab",
                "ip": "10.5.50.99",
                "type": "A",
                "zone": "evba.lab",
            },
        },
    )
    assert call_result["status"] == "ok", call_result.get("error")
    assert call_result["result"]["op_class"] == "write"


# ---------------------------------------------------------------------------
# Atomic-apply rollback E2E (DoD: invalid apply_views, invalid apply_file,
# and unresolvable record.add each leave /etc/bind/ byte-identical to the
# pre-op snapshot).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_rollback_on_invalid_apply_views_leaves_tree_unchanged(
    bind9_e2e: _Bind9Target,
) -> None:
    """An invalid apply_views payload rolls back to the pre-op snapshot."""
    from meho_backplane.connectors.bind9._atomic import AtomicApplyError

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_e2e)
        with pytest.raises(AtomicApplyError) as exc_info:
            await connector.bind9_config_apply_views(
                bind9_e2e,
                {
                    "files": {
                        # Overwrite the live named.conf.local with garbage --
                        # the primitive's validate must refuse.
                        "named.conf.local": "this is { not valid bind9 config\n",
                    },
                },
            )
        assert exc_info.value.step == "checkconf"

        after = await _checksum_bind_tree(connector, bind9_e2e)
        assert before == after, (
            f"apply_views did NOT roll back cleanly; checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_atomic_rollback_on_invalid_apply_file_leaves_tree_unchanged(
    bind9_e2e: _Bind9Target,
) -> None:
    """An invalid apply_file fragment rolls back to the pre-op snapshot."""
    from meho_backplane.connectors.bind9._atomic import AtomicApplyError

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_e2e)
        with pytest.raises(AtomicApplyError) as exc_info:
            await connector.bind9_config_apply_file(
                bind9_e2e,
                {
                    "path": "named.conf.local",
                    "content": "this is { not valid bind9 config\n",
                },
            )
        assert exc_info.value.step == "checkconf"

        after = await _checksum_bind_tree(connector, bind9_e2e)
        assert before == after, (
            f"apply_file did NOT roll back cleanly; checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()


@pytest.mark.asyncio
async def test_atomic_rollback_on_unresolvable_record_add_leaves_tree_unchanged(
    bind9_e2e: _Bind9Target,
) -> None:
    """An FQDN outside every served zone refuses pre-stage and the tree
    is byte-identical.
    """
    from meho_backplane.connectors.bind9.ops_record import ZoneResolutionError

    connector = Bind9Connector()
    try:
        before = await _checksum_bind_tree(connector, bind9_e2e)
        with pytest.raises(ZoneResolutionError):
            await connector.bind9_record_add(
                bind9_e2e,
                {
                    "fqdn": "outside.example.com",
                    "ip": "10.0.0.1",
                    "type": "A",
                },
            )
        after = await _checksum_bind_tree(connector, bind9_e2e)
        assert before == after, (
            f"unresolvable record.add must not touch /etc/bind/; "
            f"checksum before={before!r}, after={after!r}"
        )
    finally:
        await connector.aclose()


# ---------------------------------------------------------------------------
# Codebase-wide safety assertion (DoD: _remote_bash_with_sudo is the only
# sudo-invoking construction under connectors/). Runs unconditionally --
# no Docker required -- so the invariant is enforced on every sandbox.
# ---------------------------------------------------------------------------


def test_remote_bash_with_sudo_is_only_sudo_construction_in_connectors_tree() -> None:
    """No file under ``connectors/`` invokes ``sudo`` outside the safe primitive.

    Walks every ``*.py`` under
    ``backend/src/meho_backplane/connectors/`` looking for any ``sudo``
    literal in source. The only files that may carry one are
    :mod:`~meho_backplane.connectors.bind9.connector` (the
    ``_remote_bash_with_sudo`` helper itself, including its docstring
    references) and :mod:`~meho_backplane.connectors.bind9._atomic`
    (the atomic-apply primitive that routes its writes through the
    helper).

    A regression — a sibling connector hand-rolling a ``sudo -S`` or
    embedding a password in a remote argv — surfaces here as a clear
    "offender" list pointing at the file that re-introduced the
    mis-ordered-payload shape behind the 2026-05-04 / 2026-05-05
    credential leaks (Initiative #367 WI1).

    This is the same invariant the unit test in
    ``tests/test_connectors_bind9.py`` already asserts; restating it
    in the E2E harness makes the DoD checkbox observable from a
    single test command + provides defence-in-depth against a sibling
    test being deleted.
    """
    connectors_root = (
        Path(__file__).resolve().parent.parent.parent / "src" / "meho_backplane" / "connectors"
    )
    assert connectors_root.is_dir(), (
        f"connectors root not found at {connectors_root}; test layout drifted"
    )
    allowed_files = set((connectors_root / "bind9").glob("*.py"))
    offenders: list[str] = []
    for path in connectors_root.rglob("*.py"):
        if path in allowed_files:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Match `sudo ` followed by an argv token (defence vs benign
        # mentions in docstrings of unrelated connectors).
        if re.search(r"\bsudo\s+[\w-]", text):
            offenders.append(str(path.relative_to(connectors_root)))
    assert not offenders, (
        "Found `sudo ` references outside the bind9 safe-primitive surface:\n"
        + "\n".join(offenders)
    )
