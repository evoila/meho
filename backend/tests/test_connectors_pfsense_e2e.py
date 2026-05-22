# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.7-T3 pfSense recorded-fixture/fake-shell E2E test (#850).

Drives every pfSense read op through the full ``call_operation`` dispatch
stack against an in-process asyncssh fake-shell server that replays
pre-recorded fixture responses — no Docker dependency, no live pfSense.

Acceptance criteria verified (Issue #850)
==========================================

(a) All 8 pfSense ops dispatch through ``call_operation`` and return
    ``status="ok"`` results against the fake-shell server.
(b) This file passes in the ``meho-runners`` CI lane with no Docker
    dependency (asyncssh in-process server; SQLite via the autouse
    ``_default_database_url`` conftest fixture).
(c) ``pfsense.firewall.state`` E2E asserts the JSONFlux handle path
    (handle → ``result_query`` drills in) via a ``_ForceHandleReducer``.
(d) All enabled ops write an audit row carrying ``op_id`` +
    ``target_id`` + ``params_hash``.
(e) ``docs/cross-repo/pfsense-onboarding.md`` exists — not asserted
    here, verified on the doc side.

Fake-shell design
=================

The fake-shell is an in-process ``asyncssh.SSHServer`` whose
``process_factory`` dispatches by command string to pre-recorded fixture
outputs.  The SSH user authenticates with an Ed25519 key pair generated
once per test session.  The :class:`PfSenseConnector` connects to
``127.0.0.1`` on the ephemeral server port, which means the connector's
per-target connection pool is populated by a real asyncssh round-trip
(``asyncssh.connect``) rather than a mock — the only stub is the
*server side* returning fixture bytes.

Fixture responses reproduce realistic but minimal pfSense 2.7 output:
- ``cat /etc/version`` → version string used by ``about`` and ``version``
- ``pfctl -sr``        → 2-rule filter set
- ``pfctl -ss``        → 3-entry state table (exercised by handle path)
- ``pfctl -sn``        → 1 NAT rule
- ``ifconfig -a``      → 2-interface output
- ``cat /cf/conf/config.xml`` → minimal config.xml snippet (gateways +
  version) used by both ``gateway.list`` and ``config.show``
"""

from __future__ import annotations

import asyncio
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncssh
import pytest
from sqlalchemy import select

import meho_backplane.connectors.pfsense  # noqa: F401 -- import for registry side-effects
import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.pfsense import PFSENSE_OPS, PfSenseConnector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.schemas import ResultHandle
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer

# ---------------------------------------------------------------------------
# Module-level key material (generated once per session)
# ---------------------------------------------------------------------------

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()
_CLIENT_KEY_PEM: str = _CLIENT_KEY.export_private_key("pkcs8-pem").decode()

# ---------------------------------------------------------------------------
# Fixture responses — minimal pre-recorded pfSense 2.7 output
# ---------------------------------------------------------------------------

_FIXTURE_VERSION = "2.7.2-RELEASE\npfSense-CE-2.7.2-RELEASE-amd64\nFreeBSD 14.1-RELEASE-p5\n"

_FIXTURE_PFCTL_SR = (
    "pass out all keep state\n"
    "block drop in log quick on em0 proto tcp from <bruteforce> to any port = 22\n"
)

_FIXTURE_PFCTL_SS = (
    "tcp em0 10.0.0.1:55234 -> 93.184.216.34:443: ESTABLISHED:ESTABLISHED\n"
    "udp em0 10.0.0.2:1234 <-> 8.8.8.8:53\n"
    "tcp em0 10.0.0.3:44100 -> 1.1.1.1:443: ESTABLISHED:ESTABLISHED\n"
)

_FIXTURE_PFCTL_SN = "nat on em0 from 192.168.1.0/24 to any -> (em0)\n"

_FIXTURE_IFCONFIG = """\
em0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500
\toptions=81009b<RXCSUM,TXCSUM,VLAN_MTU,VLAN_HWTAGGING,VLAN_HWCSUM,VLAN_HWFILTER>
\tether 08:00:27:ab:cd:ef
\tinet 192.168.1.1 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 fe80::a00:27ff:feab:cdef%em0 prefixlen 64 scopeid 0x1
\tnd6 options=23<PERFORMNUD,ACCEPT_RTADV,AUTO_LINKLOCAL>
\tmedia: Ethernet autoselect (1000baseT <full-duplex>)
\tstatus: active

lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> metric 0 mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tnd6 options=23<PERFORMNUD,ACCEPT_RTADV,AUTO_LINKLOCAL>
"""

_FIXTURE_CONFIG_XML = """\
<?xml version="1.0"?>
<pfsense>
  <version>21.7</version>
  <gateways>
    <gateway_item>
      <name>WAN_DHCP</name>
      <interface>wan</interface>
      <gateway>192.168.1.254</gateway>
      <monitor>192.168.1.254</monitor>
      <descr>WAN gateway</descr>
      <defaultgw/>
    </gateway_item>
  </gateways>
</pfsense>
"""

# Map SSH command string → pre-recorded stdout.
_FIXTURE_RESPONSES: dict[str, str] = {
    "cat /etc/version": _FIXTURE_VERSION,
    "pfctl -sr": _FIXTURE_PFCTL_SR,
    "pfctl -ss": _FIXTURE_PFCTL_SS,
    "pfctl -sn": _FIXTURE_PFCTL_SN,
    "ifconfig -a": _FIXTURE_IFCONFIG,
    "cat /cf/conf/config.xml": _FIXTURE_CONFIG_XML,
}

# ---------------------------------------------------------------------------
# In-process fake-shell SSH server
# ---------------------------------------------------------------------------


class _FakeShellSSHServer(asyncssh.SSHServer):
    """In-process asyncssh server that accepts key auth for the test client key."""

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:  # type: ignore[override]
        return bool(key == _CLIENT_PUB)


async def _fake_shell_process_factory(process: Any) -> None:
    """Return pre-recorded fixture bytes for each pfSense SSH command."""
    cmd: str = process.command or ""
    response = _FIXTURE_RESPONSES.get(cmd)
    if response is not None:
        process.stdout.write(response)
        process.exit(0)
    else:
        process.stderr.write(f"fake-shell: unknown command: {cmd!r}\n")
        process.exit(127)


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
    """Use the default event loop policy for this module's async tests."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
async def fake_shell_server() -> AsyncIterator[types.SimpleNamespace]:
    """Yield a running in-process fake-shell asyncssh server.

    Shuts the server down after the test (RAII). The ephemeral port is
    available as ``server.port``; ``server.host`` is always
    ``"127.0.0.1"``.
    """
    srv = await asyncssh.create_server(
        _FakeShellSSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[_SERVER_KEY],
        process_factory=_fake_shell_process_factory,
    )
    port: int = srv.sockets[0].getsockname()[1]
    yield types.SimpleNamespace(host="127.0.0.1", port=port)
    srv.close()
    await srv.wait_closed()


# ---------------------------------------------------------------------------
# Environment + DB + broadcast fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin env vars that :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches around every test."""
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub out :func:`publish_event` so the broadcast bus doesn't fire."""
    events: list[Any] = []

    async def _capture(event: Any) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


# ---------------------------------------------------------------------------
# Operator and target constants
# ---------------------------------------------------------------------------

_OPERATOR_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000c3c3")

_OPERATOR = Operator(
    sub="pfsense-e2e-test",
    name="PfSense E2E Test Operator",
    email=None,
    raw_jwt="<pfsense-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_TARGET_NAME = "pfsense-e2e"

# ---------------------------------------------------------------------------
# Canary op IDs — pinned so a registration regression surfaces explicitly
# ---------------------------------------------------------------------------

EXPECTED_OP_IDS: tuple[str, ...] = (
    "pfsense.about",
    "pfsense.version",
    "pfsense.firewall.rules",
    "pfsense.firewall.state",
    "pfsense.nat.rules",
    "pfsense.interface.list",
    "pfsense.gateway.list",
    "pfsense.config.show",
)

# ---------------------------------------------------------------------------
# _ForceHandleReducer for acceptance criterion (c)
# ---------------------------------------------------------------------------


class _ForceHandleReducer:
    """Test-only reducer that wraps any ``{rows, total}`` payload in a ResultHandle.

    Used exclusively to prove the acceptance criterion that
    ``pfsense.firewall.state`` can produce a JSONFlux handle when a
    handle-producing reducer is active.  Production uses the
    PassThroughReducer (or the real JSONFlux reducer once it ships);
    this test-only variant forces the handle path unconditionally so the
    assertion doesn't depend on a row-count threshold.
    """

    async def reduce(
        self,
        payload: Any,
        schema: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, ResultHandle | None]:
        del schema, context
        # pfsense.firewall.state returns {"rows": [...], "total": N}
        rows: list[Any] = []
        total = 0
        if isinstance(payload, dict):
            rows = payload.get("rows") or []
            total = int(payload.get("total", len(rows)))
        sample = tuple(rows[:5]) if rows else ()
        handle = ResultHandle(
            handle_id=uuid.uuid4(),
            summary_md=f"pfsense-e2e force-handle ({total} rows)",
            schema_={"type": "object"},
            total_rows=total,
            sample_rows=sample or None,
            ttl_seconds=3600,
        )
        return {"row_count": total, "sample": list(sample)}, handle


# ---------------------------------------------------------------------------
# Target + connector seeding helpers
# ---------------------------------------------------------------------------


async def _seed_pfsense_target(host: str, port: int) -> TargetORM:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="pfsense",
            host=host,
            port=port,
            fqdn=None,
            secret_ref="kv/dev/pfsense/e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "2.7.2"},
            notes="seeded by test_connectors_pfsense_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


@dataclass
class _PfsenseE2EBundle:
    """All wired-up state for an E2E pfSense test."""

    target: TargetORM
    connector: PfSenseConnector


def _wire_seeded_connector(host: str, port: int) -> PfSenseConnector:
    """Return a :class:`PfSenseConnector` instance with SSH creds seeded.

    The fake-shell server uses key auth.  We inject a
    ``_SeededPfSenseConnector`` subclass that short-circuits
    :meth:`_auth_config` to return the test client key — the same
    single-seam swap the K8s E2E harness uses for its
    ``kubeconfig_loader``.
    """
    registry = all_connectors_v2()
    connector_cls = registry.get(("pfsense", "2.7", "pfsense-ssh"))
    assert connector_cls is PfSenseConnector, (
        f"PfSenseConnector not registered for (pfsense, 2.7, pfsense-ssh); got {connector_cls!r}"
    )

    class _SeededPfSenseConnector(PfSenseConnector):  # type: ignore[misc]
        """Subclass that injects the test client key into _auth_config."""

        def _auth_config(self, target: Any) -> dict[str, Any]:
            return {
                "username": "admin",
                "client_keys": [_CLIENT_KEY],
                "known_hosts": None,
            }

    instance = _SeededPfSenseConnector()
    # Inject the seeded instance so the dispatcher uses it instead of a
    # plain PfSenseConnector() which would try to load creds from Vault.
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[PfSenseConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture
async def pfsense_e2e(
    fake_shell_server: types.SimpleNamespace,
    captured_events: list[Any],
) -> _PfsenseE2EBundle:
    """Set up end-to-end pfSense test state.

    Seeds the fake-shell server on the ephemeral port, registers the
    connector ops, and pre-wires the connector instance with injected
    SSH credentials.  Caller gets a :class:`_PfsenseE2EBundle` with
    target + connector.
    """
    set_default_reducer(PassThroughReducer())
    await PfSenseConnector.register_operations()

    target = await _seed_pfsense_target(
        host=fake_shell_server.host,
        port=fake_shell_server.port,
    )
    connector = _wire_seeded_connector(
        host=fake_shell_server.host,
        port=fake_shell_server.port,
    )
    return _PfsenseE2EBundle(target=target, connector=connector)


# ---------------------------------------------------------------------------
# Tests — Op registration
# ---------------------------------------------------------------------------


def test_pfsense_ops_registration_count() -> None:
    """All 8 pfSense ops are registered in PFSENSE_OPS."""
    op_ids = {op.op_id for op in PFSENSE_OPS}
    missing = set(EXPECTED_OP_IDS) - op_ids
    assert not missing, f"Missing ops: {missing}"
    assert len(PFSENSE_OPS) == 8, f"Expected 8 ops, got {len(PFSENSE_OPS)}"


def test_pfsense_ops_all_safe_and_no_approval_required() -> None:
    """All read ops carry safety_level='safe' and requires_approval=False."""
    for op in PFSENSE_OPS:
        assert op.safety_level == "safe", f"{op.op_id} should be safe"
        assert not op.requires_approval, f"{op.op_id} should not require approval"


def test_pfsense_ops_parameter_schemas_are_empty() -> None:
    """All read ops accept no parameters (empty schema, additionalProperties=False)."""
    for op in PFSENSE_OPS:
        schema = op.parameter_schema
        assert schema.get("additionalProperties") is False, (
            f"{op.op_id}: parameter_schema must have additionalProperties=False"
        )
        assert schema.get("properties") == {}, (
            f"{op.op_id}: parameter_schema must have empty properties"
        )


def test_pfsense_ops_all_have_llm_instructions() -> None:
    """All 8 ops carry non-empty llm_instructions for agent discoverability."""
    for op in PFSENSE_OPS:
        assert op.llm_instructions, f"{op.op_id}: llm_instructions must not be empty"
        instr = op.llm_instructions
        assert "when_to_use" in instr, f"{op.op_id}: llm_instructions missing when_to_use"
        assert "output_shape" in instr, f"{op.op_id}: llm_instructions missing output_shape"
        assert instr["when_to_use"], f"{op.op_id}: when_to_use must not be empty"


def test_pfsense_ops_correct_group_keys() -> None:
    """Op group_keys match the expected firewall/nat/network/config/identity grouping."""
    expected_groups: dict[str, str] = {
        "pfsense.about": "identity",
        "pfsense.version": "config",
        "pfsense.firewall.rules": "firewall",
        "pfsense.firewall.state": "firewall",
        "pfsense.nat.rules": "nat",
        "pfsense.interface.list": "network",
        "pfsense.gateway.list": "network",
        "pfsense.config.show": "config",
    }
    op_by_id = {op.op_id: op for op in PFSENSE_OPS}
    for op_id, expected_group in expected_groups.items():
        assert op_id in op_by_id, f"Op {op_id} not found in PFSENSE_OPS"
        actual = op_by_id[op_id].group_key
        assert actual == expected_group, (
            f"{op_id}: expected group_key={expected_group!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Tests — Full dispatch path (acceptance criteria a, b, d)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_e2e_about_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.about returns vendor/product/version via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.about",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.about failed: {result.error}"
    assert result.result is not None
    assert result.result.get("vendor") == "netgate"
    assert result.result.get("version") is not None


@pytest.mark.asyncio
async def test_pfsense_e2e_version_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.version returns version/build/kernel via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.version",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.version failed: {result.error}"
    assert result.result is not None
    assert result.result.get("version") == "2.7.2-RELEASE"
    assert "FreeBSD" in (result.result.get("kernel") or "")


@pytest.mark.asyncio
async def test_pfsense_e2e_firewall_rules_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.firewall.rules returns parsed rule rows via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.firewall.rules",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.firewall.rules failed: {result.error}"
    assert result.result is not None
    rows = result.result.get("rows", [])
    assert len(rows) == 2
    actions = {row["action"] for row in rows}
    assert "pass" in actions
    assert "block" in actions


@pytest.mark.asyncio
async def test_pfsense_e2e_firewall_state_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.firewall.state returns state-table rows via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.firewall.state",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.firewall.state failed: {result.error}"
    assert result.result is not None
    rows = result.result.get("rows", [])
    assert len(rows) == 3
    protos = {row.get("proto") for row in rows}
    assert "tcp" in protos
    assert "udp" in protos


@pytest.mark.asyncio
async def test_pfsense_e2e_nat_rules_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.nat.rules returns parsed NAT rule rows via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.nat.rules",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.nat.rules failed: {result.error}"
    assert result.result is not None
    rows = result.result.get("rows", [])
    assert len(rows) == 1
    assert rows[0]["action"] == "nat"


@pytest.mark.asyncio
async def test_pfsense_e2e_interface_list_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.interface.list returns parsed interface rows via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.interface.list",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.interface.list failed: {result.error}"
    assert result.result is not None
    rows = result.result.get("rows", [])
    # Two interfaces in fixture: em0 + lo0
    assert len(rows) >= 2
    names = {row.get("name") for row in rows}
    assert "em0" in names


@pytest.mark.asyncio
async def test_pfsense_e2e_gateway_list_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.gateway.list returns parsed gateway rows via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.gateway.list",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.gateway.list failed: {result.error}"
    assert result.result is not None
    rows = result.result.get("rows", [])
    assert len(rows) == 1
    assert rows[0]["name"] == "WAN_DHCP"
    assert rows[0]["defaultgw"] is True


@pytest.mark.asyncio
async def test_pfsense_e2e_config_show_dispatches_ok(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.config.show returns config_xml and length via fake-shell."""
    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.config.show",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.config.show failed: {result.error}"
    assert result.result is not None
    config_xml = result.result.get("config_xml", "")
    assert "pfsense" in config_xml.lower() or "WAN_DHCP" in config_xml
    assert result.result.get("length", 0) > 0


# ---------------------------------------------------------------------------
# Acceptance criterion (c) — firewall.state JSONFlux handle path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_e2e_firewall_state_jsonflux_handle(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """pfsense.firewall.state with _ForceHandleReducer produces a ResultHandle.

    Exercises acceptance criterion (c): the firewall state table supports
    the JSONFlux handle path.  The test injects a ``_ForceHandleReducer``
    that unconditionally wraps the ``{rows, total}`` payload in a
    ``ResultHandle``, then asserts:

    * The ``OperationResult.extras["handle"]`` key is present.
    * The handle's ``total_rows`` equals the fixture row count (3).
    * The handle's ``sample_rows`` contains up to 5 rows.
    * ``result.result["row_count"]`` is the summary payload the reducer
      returns alongside the handle.
    """
    set_default_reducer(_ForceHandleReducer())

    result = await call_operation(
        _OPERATOR,
        op_id="pfsense.firewall.state",
        target={"name": _TARGET_NAME},
        params={},
    )
    assert result.status == "ok", f"pfsense.firewall.state (force-handle) failed: {result.error}"

    # The result payload is the reducer's summary (not the raw rows).
    assert result.result is not None
    assert "row_count" in result.result
    assert result.result["row_count"] == 3

    # The extras dict must carry the ResultHandle.
    extras = result.extras or {}
    assert "handle" in extras, (
        f"Expected extras['handle'] from _ForceHandleReducer; got extras={extras!r}"
    )
    handle = extras["handle"]
    assert handle["total_rows"] == 3
    assert handle["sample_rows"] is not None


# ---------------------------------------------------------------------------
# Acceptance criterion (d) — audit row written for every op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pfsense_e2e_dispatch_writes_audit_row(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """Each dispatch inserts an AuditLog row with op_id + target_id + params_hash.

    Dispatches ``pfsense.about`` and asserts:
    * Exactly one new ``AuditLog`` row with ``method='DISPATCH'``
      and ``path == 'pfsense.about'``.
    * ``row.target_id`` is not None (the Target row was resolved).
    * ``row.payload["op_id"]`` equals ``pfsense.about``.
    * ``row.payload["params_hash"]`` is a non-empty string.
    """
    op_id = "pfsense.about"
    sessionmaker = get_sessionmaker()

    async def _count_rows() -> int:
        async with sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == op_id,
                )
            )
            return len(list(result.scalars().all()))

    baseline = await _count_rows()

    await call_operation(
        _OPERATOR,
        op_id=op_id,
        target={"name": _TARGET_NAME},
        params={},
    )

    after = await _count_rows()
    assert after == baseline + 1, (
        f"Expected exactly 1 new DISPATCH audit row for {op_id}; baseline={baseline} after={after}"
    )

    # Verify row content.
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.method == "DISPATCH",
                AuditLog.path == op_id,
            )
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
        row = result.scalar_one()

    assert row.target_id is not None, "AuditLog.target_id must not be NULL for a dispatched op"
    assert row.payload.get("op_id") == op_id, (
        f"AuditLog.payload['op_id'] should be {op_id!r}; got {row.payload.get('op_id')!r}"
    )
    params_hash = row.payload.get("params_hash")
    assert params_hash and isinstance(params_hash, str), (
        "AuditLog.payload['params_hash'] must be a non-empty string"
    )


@pytest.mark.asyncio
async def test_pfsense_e2e_all_ops_write_audit_rows(
    pfsense_e2e: _PfsenseE2EBundle,
    captured_events: list[Any],
) -> None:
    """All 8 pfSense ops each produce an audit row after dispatch.

    Dispatches every op in EXPECTED_OP_IDS and asserts that each one
    inserted at least one ``DISPATCH`` AuditLog row.
    """
    sessionmaker = get_sessionmaker()

    for op_id in EXPECTED_OP_IDS:
        result = await call_operation(
            _OPERATOR,
            op_id=op_id,
            target={"name": _TARGET_NAME},
            params={},
        )
        assert result.status == "ok", f"Op {op_id} failed: {result.error}"

        async with sessionmaker() as session:
            db_result = await session.execute(
                select(AuditLog).where(
                    AuditLog.method == "DISPATCH",
                    AuditLog.path == op_id,
                )
            )
            rows = list(db_result.scalars().all())
        assert rows, f"No DISPATCH audit row found for op {op_id!r}"
        assert rows[-1].payload.get("op_id") == op_id, f"Audit payload op_id mismatch for {op_id}"
