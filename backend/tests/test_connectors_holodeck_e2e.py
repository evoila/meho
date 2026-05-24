# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.8-T3 Holodeck recorded-fixture / asyncssh fake-shell E2E test (#855).

Drives every Holodeck read op through the full ``call_operation`` dispatch
stack against an in-process asyncssh fake-shell server that replays
pre-recorded ``pwsh -OutputFormat Json`` fixture responses + plain-SSH
command stubs — no Docker dependency, no live HoloRouter appliance, no
published Holodeck simulator (per Initiative #371, Task #855 reference
to #536 — "no vendor CI simulator — recorded-fixture testing").

The shape mirrors the G3.7 recorded-fixture E2E precedents
(``test_connectors_pfsense_e2e.py``, ``test_connectors_gcloud_e2e.py``)
that #855's body explicitly cites as the design source.

Acceptance criteria verified (Issue #855)
==========================================

(a) ``meho holodeck --help`` lists the verb set; each verb POSTs to
    ``/api/v1/operations/call`` with the right ``op_id`` — verified
    Go-side in :file:`cli/internal/cmd/holodeck/holodeck_test.go`.
(b) ``backend/tests/test_connectors_holodeck_e2e.py`` (this file) replays
    captured pwsh / fake-shell fixtures for every enabled op and passes
    in the ``meho-runners`` CI lane with no Docker dependency (asyncssh
    in-process server; SQLite via the autouse ``_default_database_url``
    conftest fixture).
(c) ``holodeck.pod.list`` E2E asserts the JSONFlux handle path
    (handle → ``result_query`` drills in) via the real
    ``JsonFluxReducer`` in force mode (``row_threshold=0``).
(d) All enabled ops write an audit row carrying ``op_id`` + ``target_id``
    + ``params_hash``.
(e) ``docs/cross-repo/holodeck-onboarding.md`` exists — not asserted
    here, verified on the doc side.

Fake-shell design
=================

The fake-shell is an in-process ``asyncssh.SSHServer`` whose
``process_factory`` dispatches by command string to pre-recorded fixture
outputs. The SSH user authenticates with an Ed25519 key pair generated
once per test session.

The Holodeck connector sends two transport shapes through the SSH layer:

1. **Plain-SSH commands** — ``cat /etc/photon-release``,
   ``tail -n N /holodeck-runtime/logs/<component>*.log``,
   ``vtysh -c '...'``, ``cat /var/lib/dhcp/dhcpd.leases``, and the
   operator-supplied ``kubectl ...`` invocation. The fake-shell looks
   these up by full command string.

2. **pwsh-over-SSH** — the connector wraps every PowerShell cmdlet in
   ``pwsh -NoProfile -NonInteractive -EncodedCommand <b64-utf16le>``.
   The base64 payload is a moving target (each test invocation produces
   the same encoded bytes, but the per-test fixture has to be keyed by
   the *script body*, not the encoded form). The fake-shell decodes the
   ``-EncodedCommand`` argument back to the UTF-16LE script body and
   then looks the script up in :data:`_PWSH_FIXTURES`.

The :class:`HolodeckConnector` connects to ``127.0.0.1`` on the
ephemeral server port, which means the connector's per-target connection
pool is populated by a real asyncssh round-trip (``asyncssh.connect``)
rather than a mock — the only stub is the *server side* returning
fixture bytes.
"""

from __future__ import annotations

import asyncio
import base64
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncssh
import pytest
from sqlalchemy import select

import meho_backplane.connectors.holodeck  # noqa: F401 -- import for registry side-effects
import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.holodeck import HOLODECK_OPS, HolodeckConnector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer

# ---------------------------------------------------------------------------
# Module-level key material (generated once per session)
# ---------------------------------------------------------------------------

_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")
_CLIENT_PUB = _CLIENT_KEY.convert_to_public()

# ---------------------------------------------------------------------------
# Fixture responses — minimal pre-recorded Holodeck 9.0 output
# ---------------------------------------------------------------------------

# /etc/photon-release fixture text. Used by fingerprint / about / probe.
_FIXTURE_PHOTON_RELEASE = "VMware Photon Linux 5.0\nPHOTON_BUILD_NUMBER=12345\n"

# pwsh fixtures, keyed by *decoded* script body. Each entry is the JSON
# string the cmdlet would print to stdout (the Holodeck connector
# expects ``ConvertTo-Json`` output).
_PWSH_FIXTURES: dict[str, str] = {
    # holodeck.about + probe + fingerprint all run Get-HoloDeckConfig.
    # Two scripts are used: the compressed form (fingerprint/about) and
    # the depth-4 form (config.show op).
    "Get-HoloDeckConfig | ConvertTo-Json -Compress": (
        '{"Vendor":"vmware","Product":"Holodeck","Version":"9.0.0",'
        '"PodId":"HoloPod-Alpha","Build":"9.0.0-22345"}'
    ),
    "Get-HoloDeckConfig | ConvertTo-Json -Depth 4 -Compress": (
        '{"Vendor":"vmware","Product":"Holodeck","Version":"9.0.0",'
        '"PodId":"HoloPod-Alpha","Build":"9.0.0-22345",'
        '"Services":{"DHCP":"Running","DNS":"Running"}}'
    ),
    # probe runs a narrower Get-Service shape (Name,Status only) to
    # check the Holo* services are all Running.
    "Get-Service | Where-Object { $_.Name -like 'Holo*' } | "
    "Select-Object Name,Status | ConvertTo-Json": (
        '[{"Name":"HoloDhcp","Status":"Running"},{"Name":"HoloDns","Status":"Running"}]'
    ),
    # holodeck.service.list uses the depth-4, with-DisplayName shape.
    "Get-Service | Where-Object { $_.Name -like 'Holo*' } | "
    "Select-Object Name,Status,DisplayName | ConvertTo-Json -Depth 4": (
        '[{"Name":"HoloDhcp","Status":"Running","DisplayName":"Holodeck DHCP"},'
        '{"Name":"HoloDns","Status":"Running","DisplayName":"Holodeck DNS"},'
        '{"Name":"HoloFrr","Status":"Running","DisplayName":"Holodeck FRR-BGP"}]'
    ),
    # holodeck.pod.list — 3 active nested pods. Multi-element pipeline
    # → JSON array, so the {rows, total} normaliser sees a list.
    "Get-HoloDeckPod | ConvertTo-Json -Depth 4": (
        '[{"Id":"HoloPod-001","Name":"lab-A","State":"Running","Network":"Tier-0-A","VMs":4},'
        '{"Id":"HoloPod-002","Name":"lab-B","State":"Stopped","Network":"Tier-0-B","VMs":2},'
        '{"Id":"HoloPod-003","Name":"lab-C","State":"Running","Network":"Tier-0-C","VMs":3}]'
    ),
    # holodeck.pod.info — single-pod detail dict.
    "Get-HoloDeckPod -Id 'HoloPod-001' | ConvertTo-Json -Depth 4": (
        '{"Id":"HoloPod-001","Name":"lab-A","State":"Running",'
        '"Network":{"Tier0":"NSX-T0-Cluster","BGP_AS":65001},'
        '"VMs":[{"Name":"esxi-1","Power":"On"},{"Name":"esxi-2","Power":"On"}]}'
    ),
    # holodeck.networking.show — DNS zones via pwsh; BGP + DHCP via
    # plain SSH (see _PLAIN_SSH_FIXTURES below).
    "Get-DnsServerZone | Select-Object ZoneName,ZoneType | ConvertTo-Json -Depth 4": (
        '[{"ZoneName":"holodeck.lab","ZoneType":"Primary"},'
        '{"ZoneName":"vmware.local","ZoneType":"Forwarder"}]'
    ),
}

# Plain-SSH fixtures keyed by *full* command string (the Holodeck
# connector sends these without pwsh wrapping).
_PLAIN_SSH_FIXTURES: dict[str, str] = {
    "cat /etc/photon-release": _FIXTURE_PHOTON_RELEASE,
    # holodeck.logs.tail — single component, default 200 lines (the CLI
    # default; the test calls dispatch directly so `lines` is whatever
    # we pass).
    "tail -n 200 /holodeck-runtime/logs/dhcp*.log": (
        "==> /holodeck-runtime/logs/dhcp-server.log <==\n"
        "2026-05-24 09:00:01 DHCPDISCOVER from aa:bb:cc:dd:ee:ff\n"
        "2026-05-24 09:00:01 DHCPOFFER on 10.50.0.5\n"
        "2026-05-24 09:00:02 DHCPREQUEST for 10.50.0.5\n"
        "2026-05-24 09:00:02 DHCPACK on 10.50.0.5\n"
    ),
    # holodeck.networking.show — vtysh BGP summary + routes; the DNS
    # sub-section is pwsh (above), DHCP is the leases file (below).
    "vtysh -c 'show bgp summary'": (
        "IPv4 Unicast Summary:\n"
        "BGP router identifier 10.0.0.1, local AS number 65001\n"
        "Neighbor        V         AS    MsgRcvd    MsgSent  Up/Down  State/PfxRcd\n"
        "10.0.0.2        4      65002        100        100  00:30:00            5\n"
    ),
    "vtysh -c 'show ip route'": (
        "Codes: K - kernel route, C - connected, S - static, R - RIP,\n"
        "       O - OSPF, I - IS-IS, B - BGP\n"
        "C>* 10.0.0.0/24 is directly connected, eth0\n"
        "B>* 10.50.0.0/24 [20/0] via 10.0.0.2, eth0, 00:30:00\n"
    ),
    "cat /var/lib/dhcp/dhcpd.leases": (
        "lease 10.50.0.5 {\n"
        "  starts 4 2026/05/24 09:00:02;\n"
        "  ends 4 2026/05/24 21:00:02;\n"
        "  hardware ethernet aa:bb:cc:dd:ee:ff;\n"
        '  client-hostname "esxi-1";\n'
        "}\n"
    ),
    # holodeck.k8s.exec — verbatim kubectl forward. Two fixtures here:
    # the safe `kubectl get pods` invocation that the read-only safelist
    # accepts, and an alternate `kubectl get nodes` for the second test
    # case. Mutating verbs / shell-metachar payloads are rejected
    # **before** they reach the fake-shell, by the backend's
    # parse_kubectl_command guard, so we don't need fixtures for them.
    "kubectl get pods -n holodeck": (
        "NAME                    READY   STATUS    RESTARTS   AGE\n"
        "holo-dhcp-7d4f9c-abc12   1/1     Running   0          5d\n"
        "holo-dns-5b6c7d-xyz45    1/1     Running   0          5d\n"
    ),
    "kubectl get nodes": (
        "NAME             STATUS   ROLES                  AGE   VERSION\n"
        "holorouter-1     Ready    control-plane,master   30d   v1.29.3\n"
    ),
}

# ---------------------------------------------------------------------------
# In-process fake-shell SSH server
# ---------------------------------------------------------------------------


def _decode_encoded_command(cmd: str) -> str | None:
    """Decode the ``-EncodedCommand <b64>`` argument back to the script body.

    The Holodeck pwsh helper sends commands in the form
    ``pwsh -NoProfile -NonInteractive -EncodedCommand <b64-utf16le>``.
    The fake-shell needs to recover the *script body* (a stable
    fixture key) from the encoded form (per-call shape but deterministic
    for the same input).

    Returns ``None`` for non-pwsh commands or malformed encoding so
    the caller falls through to the plain-SSH fixture lookup.
    """
    if "-EncodedCommand " not in cmd:
        return None
    encoded = cmd.rsplit("-EncodedCommand ", 1)[1].strip()
    try:
        return base64.b64decode(encoded).decode("utf-16-le")
    except (ValueError, UnicodeDecodeError):
        return None


class _FakeShellSSHServer(asyncssh.SSHServer):
    """In-process asyncssh server that accepts key auth for the test client key."""

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: Any) -> bool:  # type: ignore[override]
        return bool(key == _CLIENT_PUB)


async def _fake_shell_process_factory(process: Any) -> None:
    """Dispatch by command string to pre-recorded fixture bytes.

    Three lookup paths:

    1. The command starts with ``pwsh ... -EncodedCommand``: decode the
       b64 to recover the PowerShell script body and look up in
       :data:`_PWSH_FIXTURES`.
    2. The command matches a plain-SSH fixture key verbatim
       (:data:`_PLAIN_SSH_FIXTURES`).
    3. Neither path matches — return exit 127 + an explicit
       "unknown command" stderr so the test failure is operator-readable.
    """
    cmd: str = process.command or ""

    decoded_script = _decode_encoded_command(cmd)
    if decoded_script is not None:
        response = _PWSH_FIXTURES.get(decoded_script)
        if response is not None:
            process.stdout.write(response)
            process.exit(0)
            return
        process.stderr.write(f"fake-shell: unknown pwsh script:\n{decoded_script!r}\n")
        process.exit(127)
        return

    response = _PLAIN_SSH_FIXTURES.get(cmd)
    if response is not None:
        process.stdout.write(response)
        process.exit(0)
        return

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

_OPERATOR_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-00000000ddee")

_OPERATOR = Operator(
    sub="holodeck-e2e-test",
    name="Holodeck E2E Test Operator",
    email=None,
    raw_jwt="<holodeck-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_TARGET_NAME = "holodeck-e2e"

# ---------------------------------------------------------------------------
# Canary op IDs — pinned so a registration regression surfaces explicitly
# ---------------------------------------------------------------------------

EXPECTED_OP_IDS: tuple[str, ...] = (
    "holodeck.about",
    "holodeck.config.show",
    "holodeck.pod.list",
    "holodeck.pod.info",
    "holodeck.service.list",
    "holodeck.k8s.exec",
    "holodeck.logs.tail",
    "holodeck.networking.show",
)

# ---------------------------------------------------------------------------
# Target + connector seeding helpers
# ---------------------------------------------------------------------------


async def _seed_holodeck_target(host: str, port: int) -> TargetORM:
    """Insert the E2E target row and return it (expunged from the session)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="holodeck",
            host=host,
            port=port,
            fqdn=None,
            secret_ref="kv/dev/holodeck/e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "9.0.0"},
            notes="seeded by test_connectors_holodeck_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


@dataclass
class _HolodeckE2EBundle:
    """All wired-up state for an E2E Holodeck test."""

    target: TargetORM
    connector: HolodeckConnector


def _wire_seeded_connector(host: str, port: int) -> HolodeckConnector:
    """Return a :class:`HolodeckConnector` instance with SSH creds seeded.

    The fake-shell server uses key auth. We inject a
    ``_SeededHolodeckConnector`` subclass that short-circuits
    :meth:`_auth_config` to return the test client key — the same
    single-seam swap the pfsense / K8s E2E harnesses use.
    """
    del host, port  # connection params come from the seeded target row
    registry = all_connectors_v2()
    connector_cls = registry.get(("holodeck", "9.0", "holodeck-ssh"))
    assert connector_cls is HolodeckConnector, (
        f"HolodeckConnector not registered for (holodeck, 9.0, holodeck-ssh); got {connector_cls!r}"
    )

    class _SeededHolodeckConnector(HolodeckConnector):  # type: ignore[misc]
        """Subclass that injects the test client key into _auth_config."""

        async def _auth_config(self, target: Any) -> dict[str, Any]:
            return {
                "username": "root",
                "client_keys": [_CLIENT_KEY],
                "known_hosts": None,
            }

    instance = _SeededHolodeckConnector()
    # Inject the seeded instance so the dispatcher uses it instead of a
    # plain HolodeckConnector() which would try to load creds from Vault.
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[HolodeckConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture
async def holodeck_e2e(
    fake_shell_server: types.SimpleNamespace,
    captured_events: list[Any],
) -> AsyncIterator[_HolodeckE2EBundle]:
    """Set up end-to-end Holodeck test state.

    Seeds the fake-shell server on the ephemeral port, registers the
    connector ops, and pre-wires the connector instance with injected
    SSH credentials. Caller gets a :class:`_HolodeckE2EBundle` with
    target + connector.

    Teardown: closes the connector's SSH connection pool so asyncssh
    background reader tasks do not leak into subsequent tests and block
    the fake-shell server teardown (``srv.wait_closed``).
    """
    del captured_events  # autouse via parameter; reading drains the stub
    set_default_reducer(PassThroughReducer())
    await HolodeckConnector.register_operations()

    target = await _seed_holodeck_target(
        host=fake_shell_server.host,
        port=fake_shell_server.port,
    )
    connector = _wire_seeded_connector(
        host=fake_shell_server.host,
        port=fake_shell_server.port,
    )
    yield _HolodeckE2EBundle(target=target, connector=connector)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Tests — Op registration
# ---------------------------------------------------------------------------


def test_holodeck_ops_registration_count() -> None:
    """All 8 Holodeck ops are registered in HOLODECK_OPS."""
    op_ids = {op.op_id for op in HOLODECK_OPS}
    missing = set(EXPECTED_OP_IDS) - op_ids
    assert not missing, f"Missing ops: {missing}"
    assert len(HOLODECK_OPS) == 8, f"Expected 8 ops, got {len(HOLODECK_OPS)}"


def test_holodeck_ops_all_safe_and_no_approval_required() -> None:
    """All read ops carry safety_level='safe' and requires_approval=False."""
    for op in HOLODECK_OPS:
        assert op.safety_level == "safe", f"{op.op_id} should be safe"
        assert not op.requires_approval, f"{op.op_id} should not require approval"


def test_holodeck_ops_all_have_llm_instructions() -> None:
    """All 8 ops carry non-empty llm_instructions for agent discoverability."""
    for op in HOLODECK_OPS:
        assert op.llm_instructions, f"{op.op_id}: llm_instructions must not be empty"
        instr = op.llm_instructions
        assert "when_to_use" in instr, f"{op.op_id}: llm_instructions missing when_to_use"
        assert "output_shape" in instr, f"{op.op_id}: llm_instructions missing output_shape"
        assert instr["when_to_use"], f"{op.op_id}: when_to_use must not be empty"


def test_holodeck_ops_all_carry_ssh_only_transport_note() -> None:
    """MCP review acceptance criterion (#855): every op's `when_to_use`
    surfaces the canonical "Holodeck has no REST API; transport is
    PowerShell-over-SSH" note so agents don't compose against a
    non-existent REST surface.

    The exact phrase to look for is "Holodeck has no REST API"; the
    shared :data:`SSH_TRANSPORT_NOTE` constant guarantees consistency
    across all 8 ops.
    """
    canonical_phrase = "Holodeck has no REST API"
    pwsh_phrase = "PowerShell-over-SSH"
    for op in HOLODECK_OPS:
        when_to_use = (op.llm_instructions or {}).get("when_to_use", "")
        assert canonical_phrase in when_to_use, (
            f"{op.op_id}: llm_instructions.when_to_use missing canonical SSH-only "
            f"transport note; expected substring {canonical_phrase!r} so an LLM "
            f"reading the op metadata understands Holodeck has no REST surface. "
            f"Got: {when_to_use!r}"
        )
        assert pwsh_phrase in when_to_use, (
            f"{op.op_id}: llm_instructions.when_to_use missing PowerShell-over-SSH "
            f"phrase; the agent needs to know which transport carries cmdlet ops."
        )


def test_holodeck_ops_correct_group_keys() -> None:
    """Op group_keys match the expected identity/config/pod/service/k8s/logs/networking grouping."""
    expected_groups: dict[str, str] = {
        "holodeck.about": "identity",
        "holodeck.config.show": "config",
        "holodeck.pod.list": "pod",
        "holodeck.pod.info": "pod",
        "holodeck.service.list": "service",
        "holodeck.k8s.exec": "k8s",
        "holodeck.logs.tail": "logs",
        "holodeck.networking.show": "networking",
    }
    op_by_id = {op.op_id: op for op in HOLODECK_OPS}
    for op_id, expected_group in expected_groups.items():
        assert op_id in op_by_id, f"Op {op_id} not found in HOLODECK_OPS"
        actual = op_by_id[op_id].group_key
        assert actual == expected_group, (
            f"{op_id}: expected group_key={expected_group!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Tests — Full dispatch path (acceptance criteria a, b, d)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holodeck_e2e_about_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.about returns vendor/product/version via fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.about",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"holodeck.about failed: {result.get('error')}"
    assert result.get("result") is not None
    assert result["result"].get("vendor") == "vmware"
    assert result["result"].get("product") == "holodeck"
    assert result["result"].get("version") == "9.0.0"
    assert result["result"].get("photon_version") == "5.0"
    assert result["result"].get("pod_id") == "HoloPod-Alpha"


@pytest.mark.asyncio
async def test_holodeck_e2e_config_show_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.config.show returns the full config dict via fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.config.show",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"holodeck.config.show failed: {result.get('error')}"
    cfg = result["result"].get("config")
    assert isinstance(cfg, dict), f"Expected dict config; got {cfg!r}"
    assert cfg.get("Version") == "9.0.0"
    assert "Services" in cfg


@pytest.mark.asyncio
async def test_holodeck_e2e_pod_list_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.pod.list returns the {rows, total} envelope via fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.pod.list",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"holodeck.pod.list failed: {result.get('error')}"
    rows = result["result"].get("rows", [])
    assert len(rows) == 3, f"Expected 3 pods in fixture; got {len(rows)}"
    ids = {r.get("Id") for r in rows}
    assert ids == {"HoloPod-001", "HoloPod-002", "HoloPod-003"}


@pytest.mark.asyncio
async def test_holodeck_e2e_pod_info_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.pod.info returns single-pod detail via fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.pod.info",
            "target": {"name": _TARGET_NAME},
            "params": {"pod_id": "HoloPod-001"},
        },
    )
    assert result["status"] == "ok", f"holodeck.pod.info failed: {result.get('error')}"
    pod = result["result"].get("pod")
    assert isinstance(pod, dict)
    assert pod.get("Id") == "HoloPod-001"
    assert pod.get("State") == "Running"


@pytest.mark.asyncio
async def test_holodeck_e2e_service_list_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.service.list returns {rows, total} of Holo* services."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.service.list",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"holodeck.service.list failed: {result.get('error')}"
    rows = result["result"].get("rows", [])
    assert len(rows) == 3
    names = {r.get("Name") for r in rows}
    assert "HoloDhcp" in names
    assert "HoloDns" in names


@pytest.mark.asyncio
async def test_holodeck_e2e_k8s_exec_safe_verb_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.k8s.exec with a read-only verb (`kubectl get pods`)
    succeeds and returns the fixture stdout. This exercises the full
    safety path: the schema-layer pattern accepts the shape, the
    handler-layer `parse_kubectl_command` accepts the verb, and the
    fake-shell returns the fixture stdout.
    """
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.k8s.exec",
            "target": {"name": _TARGET_NAME},
            "params": {"command": "kubectl get pods -n holodeck"},
        },
    )
    assert result["status"] == "ok", f"holodeck.k8s.exec failed: {result.get('error')}"
    out = result["result"].get("stdout", "")
    assert "holo-dhcp" in out, f"Expected fixture stdout; got {out!r}"
    assert result["result"].get("exit_status") == 0


@pytest.mark.asyncio
async def test_holodeck_e2e_k8s_exec_metachar_rejected(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """E2E regression for the iter-2 shell-injection fix on T2 (#1005).

    A command containing a POSIX-shell metacharacter (``;``) must be
    refused **before** any SSH traffic happens. The handler returns
    status="ok" with an inline `error` string in the result envelope
    (because the safety check raises KubectlSafetyError, which the
    bound-method handler folds into the error envelope rather than
    raising into result_connector_error).

    This is the load-bearing regression test: if a future schema
    widening or handler refactor accidentally re-opens the chained-shell
    hole, the fixture lookup in the fake-shell would *succeed* against
    a stub for the safe prefix (`kubectl get pods`) and the chained tail
    (`rm -rf /`) would be forwarded to the appliance. The test guards
    against that by asserting (a) the call returns status="ok" with an
    inline error string, (b) the fixture stdout is NOT in the result
    (no SSH round-trip happened), and (c) the error mentions
    "metacharacter".
    """
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.k8s.exec",
            "target": {"name": _TARGET_NAME},
            "params": {"command": "kubectl get pods; rm -rf /"},
        },
    )
    # The schema-layer pattern rejects the shape first → result is
    # status=error from result_invalid_params. (The handler-layer
    # metachar reject is the load-bearing gate but the schema layer
    # catches this shape earlier.) Either status is acceptable: what
    # matters is that the fake-shell's safe-prefix fixture stdout
    # NEVER lands in the response envelope.
    fake_shell_stdout_marker = "holo-dhcp"
    result_str = repr(result)
    assert fake_shell_stdout_marker not in result_str, (
        "Safe-prefix fixture stdout leaked into the response — the "
        "metachar reject failed closed; the chained `rm -rf /` would "
        "have been forwarded to the appliance shell."
    )
    # Either the schema layer (status=error) or the handler layer
    # (status=ok with inline error) refused it; both are correct.
    if result["status"] == "ok":
        err_str = result["result"].get("error") or ""
        assert err_str, "status=ok requires an inline error string from the safety check"
        err_lower = err_str.lower()
        assert "metacharacter" in err_lower or "safety" in err_lower, (
            f"Expected safety-check error mention; got {err_str!r}"
        )
    else:
        assert result["status"] in ("error", "denied"), (
            f"Unexpected status for metachar-rejected call: {result['status']}"
        )


@pytest.mark.asyncio
async def test_holodeck_e2e_logs_tail_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.logs.tail returns the parsed file tail via fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.logs.tail",
            "target": {"name": _TARGET_NAME},
            "params": {"component": "dhcp", "lines": 200},
        },
    )
    assert result["status"] == "ok", f"holodeck.logs.tail failed: {result.get('error')}"
    files = result["result"].get("files", [])
    assert len(files) == 1
    assert "dhcp-server.log" in (files[0].get("path") or "")
    assert "DHCPDISCOVER" in (files[0].get("lines") or "")
    assert result["result"].get("lines_requested") == 200


@pytest.mark.asyncio
async def test_holodeck_e2e_networking_show_dispatches_ok(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.networking.show composes the BGP + routes + DNS + DHCP envelope."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": "holodeck.networking.show",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"holodeck.networking.show failed: {result.get('error')}"
    envelope = result["result"]
    assert envelope["bgp"]["ok"] is True
    assert "BGP router identifier" in envelope["bgp"]["summary_text"]
    assert envelope["routes"]["ok"] is True
    assert "B>" in envelope["routes"]["text"]
    assert envelope["dns"]["ok"] is True
    assert envelope["dns"]["total"] == 2
    zone_names = {z.get("ZoneName") for z in envelope["dns"]["zones"]}
    assert "holodeck.lab" in zone_names
    assert envelope["dhcp"]["ok"] is True
    assert "lease 10.50.0.5" in envelope["dhcp"]["leases_text"]


# ---------------------------------------------------------------------------
# Acceptance criterion (c) — pod.list JSONFlux handle path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holodeck_e2e_pod_list_jsonflux_handle(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """holodeck.pod.list with the real JsonFluxReducer produces a ResultHandle.

    Exercises acceptance criterion (c): the pod list supports the
    JSONFlux handle path. The test installs the real
    :class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
    in force mode (``row_threshold=0``) so the seeded 3-row pod list
    materialises into a handle, then asserts:

    * The ``OperationResult.handle`` (top-level ``result["handle"]``) is
      present and non-None.
    * The handle's ``total_rows`` equals the fixture row count (3).
    * The handle's ``sample_rows`` is populated.
    * ``result.result["row_count"]`` is the summary payload the reducer
      returns alongside the handle.

    Teardown restores :class:`PassThroughReducer` so a follow-on test
    in the same session sees the v0.2 default.
    """
    del captured_events
    set_default_reducer(JsonFluxReducer(row_threshold=0))
    try:
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": "holodeck-ssh-9.0",
                "op_id": "holodeck.pod.list",
                "target": {"name": _TARGET_NAME},
                "params": {},
            },
        )
        assert result["status"] == "ok", (
            f"holodeck.pod.list (force-handle) failed: {result.get('error')}"
        )

        # The result payload is the reducer's summary (not the raw rows).
        assert result.get("result") is not None
        assert "row_count" in result["result"]
        assert result["result"]["row_count"] == 3

        # The top-level "handle" key must carry the ResultHandle.
        handle = result.get("handle")
        assert handle is not None, (
            f"Expected result['handle'] from JsonFluxReducer; got result={result!r}"
        )
        assert handle["total_rows"] == 3
        assert handle["sample_rows"] is not None
    finally:
        set_default_reducer(PassThroughReducer())
        reset_dispatcher_caches()


# ---------------------------------------------------------------------------
# Acceptance criterion (d) — audit row written for every op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holodeck_e2e_dispatch_writes_audit_row(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """Each dispatch inserts an AuditLog row with op_id + target_id + params_hash.

    Dispatches ``holodeck.about`` and asserts:
    * Exactly one new ``AuditLog`` row with ``method='DISPATCH'``
      and ``path == 'holodeck.about'``.
    * ``row.target_id`` is not None (the Target row was resolved).
    * ``row.payload["op_id"]`` equals ``holodeck.about``.
    * ``row.payload["params_hash"]`` is a non-empty string.
    """
    del captured_events
    op_id = "holodeck.about"
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
        {
            "connector_id": "holodeck-ssh-9.0",
            "op_id": op_id,
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )

    after = await _count_rows()
    assert after == baseline + 1, (
        f"Expected exactly 1 new DISPATCH audit row for {op_id}; baseline={baseline} after={after}"
    )

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
async def test_holodeck_e2e_all_ops_write_audit_rows(
    holodeck_e2e: _HolodeckE2EBundle,
    captured_events: list[Any],
) -> None:
    """All 8 Holodeck ops each produce an audit row after dispatch.

    Dispatches every op in EXPECTED_OP_IDS and asserts that each one
    inserted at least one ``DISPATCH`` AuditLog row.
    """
    del captured_events
    sessionmaker = get_sessionmaker()

    # Params each op needs (everything else is empty).
    params_for_op: dict[str, dict[str, Any]] = {
        "holodeck.pod.info": {"pod_id": "HoloPod-001"},
        "holodeck.k8s.exec": {"command": "kubectl get nodes"},
        "holodeck.logs.tail": {"component": "dhcp", "lines": 200},
    }

    for op_id in EXPECTED_OP_IDS:
        params = params_for_op.get(op_id, {})
        result = await call_operation(
            _OPERATOR,
            {
                "connector_id": "holodeck-ssh-9.0",
                "op_id": op_id,
                "target": {"name": _TARGET_NAME},
                "params": params,
            },
        )
        assert result["status"] == "ok", f"Op {op_id} failed: {result.get('error')}"

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
