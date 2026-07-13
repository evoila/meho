# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RKE2 connector recorded-fixture / asyncssh fake-shell E2E test (#2221).

Drives ``rke2.about``, ``rke2.posture.show`` and the safe, non-gated
``rke2.etcd-snapshot.save`` (T4 #2431) through the full ``call_operation``
dispatch stack against an in-process asyncssh fake-shell server that
replays plain-SSH command stubs -- no Docker dependency, no live RKE2
node. The shape mirrors the G3.8 Holodeck recorded-fixture E2E precedent
(``test_connectors_holodeck_e2e.py``).

Acceptance criteria verified (Issue #2221)
==========================================

* A ``rke2.about`` / ``rke2.posture.show`` dispatch via ``call_operation``
  against a registered target returns ``status="ok"`` -- the connector +
  read-posture tier are dispatchable (AC1).
* The posture result reports config-file modes + the join-token presence
  with the token **value never present** (redacted); the ``stat`` command
  is the only transport touched -- no ``cat`` of the token path (AC1/AC3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncssh
import pytest

import meho_backplane.connectors.rke2  # noqa: F401 -- import for registry side-effects
import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.rke2 import Rke2SshConnector
from meho_backplane.db.engine import get_sessionmaker
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

# ---------------------------------------------------------------------------
# Fixture responses -- minimal pre-recorded RKE2 node output
# ---------------------------------------------------------------------------

_OS_RELEASE_FIXTURE = 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.3 LTS"\nID=ubuntu\n'
_RKE2_VERSION_FIXTURE = "rke2 version v1.29.3+rke2r1 (a1b2c3d)\ngo version go1.21.8\n"
# stat -c '%n|%a|%U|%G' output: config.yaml + rke2.yaml + on-disk token.
# The token line carries its MODE only -- the value is never read.
_STAT_FIXTURE = (
    "/etc/rancher/rke2/config.yaml|600|root|root\n"
    "/etc/rancher/rke2/rke2.yaml|600|root|root\n"
    "/var/lib/rancher/rke2/server/token|600|root|root\n"
)
# Precondition-guard sentinel for an embedded-etcd server node.
_SNAPSHOT_GUARD_FIXTURE = "ok\n"
# `rke2 etcd-snapshot save` logs the saved snapshot name to stderr.
_SNAPSHOT_SAVE_FIXTURE = "INFO[0000] Snapshot pre-upgrade-rke2-node-e2e-1754907117 saved.\n"

# A canary token value the fixture never emits (posture never reads the
# token content). Asserted absent from every dispatch result.
_TOKEN_VALUE_CANARY = "K10rke2e2ecanarytokenDONOTLEAK::server:zzz999"  # NOSONAR


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
    """Dispatch by command string (prefix-matched) to pre-recorded bytes."""
    cmd: str = process.command or ""
    if cmd == "cat /etc/os-release":
        response: str | None = _OS_RELEASE_FIXTURE
    elif cmd.startswith("rke2 --version"):
        response = _RKE2_VERSION_FIXTURE
    elif cmd.startswith("stat -c '%n|%a|%U|%G'"):
        response = _STAT_FIXTURE
    elif cmd.startswith("printf 'ACTIVE="):
        # rke2.token.rotate fingerprint preflight: server node, active,
        # patched version (1.29 is above the CVE-fix range).
        response = (
            "ACTIVE=active\nUNIT=rke2-server.service\n"
            "VERSION=rke2 version v1.29.3+rke2r2 (a1b2c3d)\n"
        )
    elif cmd.startswith("/var/lib/rancher/rke2/bin/rke2 etcd-snapshot save"):
        # rke2.etcd-snapshot.save -- run as root over plain SSH (no sudo argv).
        # rke2 logs the saved-snapshot line to stderr; stdout stays empty.
        process.stderr.write(_SNAPSHOT_SAVE_FIXTURE)
        process.exit(0)
        return
    elif cmd.startswith("sh -c "):
        # etcd-snapshot precondition guard (plain, as root) -- report an
        # embedded-etcd server node.
        response = _SNAPSHOT_GUARD_FIXTURE
    elif cmd.startswith("set -e; umask 077; f=$(mktemp)"):
        # The safe-sudo wire command streams the script + password on stdin;
        # drain it (the real mktemp pipeline would consume it) so the client
        # write side doesn't break, then simulate a successful
        # `rke2 token rotate` (exit 0, no stdout, never echoing a token).
        with contextlib.suppress(Exception):
            await process.stdin.read()
        process.exit(0)
        return
    else:
        process.stderr.write(f"fake-shell: unknown command: {cmd!r}\n")
        process.exit(127)
        return
    process.stdout.write(response)
    process.exit(0)


@pytest.fixture(scope="module")
def event_loop_policy() -> asyncio.DefaultEventLoopPolicy:
    """Use the default event loop policy for this module's async tests."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
async def fake_shell_server() -> AsyncIterator[types.SimpleNamespace]:
    """Yield a running in-process fake-shell asyncssh server."""
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
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    from meho_backplane.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
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
# Operator + target constants
# ---------------------------------------------------------------------------

_OPERATOR_TENANT_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-0000000042ee")

_OPERATOR = Operator(
    sub="rke2-e2e-test",
    name="RKE2 E2E Test Operator",
    email=None,
    raw_jwt="<rke2-e2e-raw-jwt>",
    tenant_id=_OPERATOR_TENANT_ID,
    tenant_role=TenantRole.TENANT_ADMIN,
)

_TARGET_NAME = "rke2-node-e2e"
_CONNECTOR_ID = "rke2-ssh-1.x"


# ---------------------------------------------------------------------------
# Target + connector seeding helpers
# ---------------------------------------------------------------------------


async def _seed_rke2_target(host: str, port: int) -> TargetORM:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        target = TargetORM(
            tenant_id=_OPERATOR_TENANT_ID,
            name=_TARGET_NAME,
            aliases=[],
            product="rke2",
            host=host,
            port=port,
            fqdn=None,
            secret_ref="kv/dev/rke2/e2e",
            auth_model="shared_service_account",
            vpn_required=False,
            extras={},
            fingerprint={"version": "1.x"},
            notes="seeded by test_connectors_rke2_e2e",
        )
        session.add(target)
        await session.commit()
        await session.refresh(target)
        session.expunge(target)
        return target


@dataclass
class _Rke2E2EBundle:
    target: TargetORM
    connector: Rke2SshConnector


def _wire_seeded_connector() -> Rke2SshConnector:
    """Return a :class:`Rke2SshConnector` with the test client key injected."""
    registry = all_connectors_v2()
    connector_cls = registry.get(("rke2", "1.x", "rke2-ssh"))
    assert connector_cls is Rke2SshConnector, (
        f"Rke2SshConnector not registered for (rke2, 1.x, rke2-ssh); got {connector_cls!r}"
    )

    class _SeededRke2Connector(Rke2SshConnector):  # type: ignore[misc]
        async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
            return {
                "username": "root",
                "client_keys": [_CLIENT_KEY],
                "known_hosts": None,
            }

    instance = _SeededRke2Connector()
    from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE

    _CONNECTOR_INSTANCE_CACHE[Rke2SshConnector] = instance  # type: ignore[assignment]
    return instance


@pytest.fixture
async def rke2_e2e(
    fake_shell_server: types.SimpleNamespace,
    captured_events: list[Any],
) -> AsyncIterator[_Rke2E2EBundle]:
    del captured_events  # autouse via parameter; reading drains the stub
    set_default_reducer(PassThroughReducer())
    await Rke2SshConnector.register_operations()

    target = await _seed_rke2_target(
        host=fake_shell_server.host,
        port=fake_shell_server.port,
    )
    connector = _wire_seeded_connector()
    yield _Rke2E2EBundle(target=target, connector=connector)
    await connector.aclose()


# ---------------------------------------------------------------------------
# Tests -- full dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rke2_e2e_about_dispatches_ok(
    rke2_e2e: _Rke2E2EBundle,
    captured_events: list[Any],
) -> None:
    """rke2.about returns vendor/product/version/node_os via the fake-shell."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": "rke2.about",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"rke2.about failed: {result.get('error')}"
    payload = result["result"]
    assert payload["vendor"] == "rancher"
    assert payload["product"] == "rke2"
    assert payload["version"] == "v1.29.3+rke2r1"
    assert payload["node_os"] == "Ubuntu 22.04.3 LTS"


@pytest.mark.asyncio
async def test_rke2_e2e_posture_show_dispatches_ok_and_redacts(
    rke2_e2e: _Rke2E2EBundle,
    captured_events: list[Any],
) -> None:
    """rke2.posture.show returns config modes + a redacted token entry."""
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": "rke2.posture.show",
            "target": {"name": _TARGET_NAME},
            "params": {},
        },
    )
    assert result["status"] == "ok", f"rke2.posture.show failed: {result.get('error')}"
    envelope = result["result"]
    cfg = {c["path"]: c for c in envelope["config_files"]}
    assert cfg["/etc/rancher/rke2/config.yaml"]["mode"] == "0600"
    assert cfg["/etc/rancher/rke2/config.yaml"]["present"] is True
    assert cfg["/etc/rancher/rke2/rke2.yaml"]["mode"] == "0600"
    token = envelope["token"]
    assert token["path"] == "/var/lib/rancher/rke2/server/token"
    assert token["present"] is True
    assert token["mode"] == "0600"
    assert token["redacted"] is True
    # Redaction: no token VALUE anywhere in the dispatch result.
    assert _TOKEN_VALUE_CANARY not in repr(result)
    assert "value" not in token


@pytest.mark.asyncio
async def test_rke2_e2e_etcd_snapshot_save_dispatches_ok_non_gated(
    rke2_e2e: _Rke2E2EBundle,
    captured_events: list[Any],
) -> None:
    """rke2.etcd-snapshot.save (safe, non-gated) auto-executes -- no approval park.

    A TENANT_ADMIN dispatch of the safe, non-approval snapshot op runs to
    completion through the full ``call_operation`` stack: it does NOT park
    at ``awaiting_approval`` (that gate is only for the sibling write ops),
    and the result carries the parsed snapshot name + path with exit 0.
    """
    del captured_events
    result = await call_operation(
        _OPERATOR,
        {
            "connector_id": _CONNECTOR_ID,
            "op_id": "rke2.etcd-snapshot.save",
            "target": {"name": _TARGET_NAME},
            "params": {"name": "pre-upgrade"},
        },
    )
    # Non-gated: the dispatch executes rather than parking for approval.
    assert result["status"] == "ok", f"rke2.etcd-snapshot.save did not auto-execute: {result}"
    payload = result["result"]
    assert payload["snapshot_name"] == "pre-upgrade-rke2-node-e2e-1754907117"
    assert payload["path"] == (
        "/var/lib/rancher/rke2/server/db/snapshots/pre-upgrade-rke2-node-e2e-1754907117"
    )
    assert payload["exit_status"] == 0


# ---------------------------------------------------------------------------
# rke2.token.rotate -- park -> approve -> execute -> audit (#2429)
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402

from sqlalchemy import select  # noqa: E402

from meho_backplane.db.models import AuditLog  # noqa: E402
from meho_backplane.operations import dispatch  # noqa: E402
from meho_backplane.targets.resolver import resolve_target  # noqa: E402

# The minted token the handler generates on the approved path. Patched to a
# fixed canary so the audit-row assertion can prove it never lands there.
_MINTED_TOKEN_CANARY = "K10E2Ecanaryminted00000000deadbeefMUSTNOTLEAK"  # gitleaks:allow NOSONAR


def _fake_vault_write(version: int = 3):
    """Patch vault_client_for_operator to a fake async-CM KV-v2 writer."""
    client = MagicMock()
    client.secrets.kv.v2.create_or_update_secret = MagicMock(
        return_value={"data": {"version": version}}
    )

    @contextlib.asynccontextmanager
    async def _fake_client(operator: Any):
        yield client

    return patch("meho_backplane.auth.vault.vault_client_for_operator", _fake_client)


async def _dispatch_rotate(bundle: _Rke2E2EBundle, *, approved: bool) -> dict[str, Any]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        resolved = await resolve_target(session, _OPERATOR.tenant_id, _TARGET_NAME)
    # The sudo credential resolves off the target's Vault secret; the seeded
    # connector's _auth_config doesn't cover _resolve_secret, so stub it.
    with patch.object(
        bundle.connector,
        "_resolve_secret",
        new=_AsyncReturn({"username": "root", "password": "e2e-sudo-pw"}),
    ):
        result = await dispatch(
            operator=_OPERATOR,
            connector_id=_CONNECTOR_ID,
            op_id="rke2.token.rotate",
            target=resolved,
            params={},
            _approved=approved,
        )
    return result.model_dump(mode="json")


class _AsyncReturn:
    """Minimal awaitable-returning stand-in for an async method patch."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._value


@pytest.mark.asyncio
async def test_rke2_token_rotate_parks_for_user(
    rke2_e2e: _Rke2E2EBundle,
    captured_events: list[Any],
) -> None:
    """An un-approved USER dispatch parks (not run, not hard-denied)."""
    del captured_events
    result = await _dispatch_rotate(rke2_e2e, approved=False)
    assert result["status"] == "awaiting_approval", result
    assert result.get("extras", {}).get("approval_request_id")


@pytest.mark.asyncio
async def test_rke2_token_rotate_approved_audit_row_has_no_token(
    rke2_e2e: _Rke2E2EBundle,
    captured_events: list[Any],
) -> None:
    """AC: approved resume rotates + stashes to Vault; the raw audit row has NO token."""
    del captured_events
    op_id = "rke2.token.rotate"
    with (
        _fake_vault_write(version=5),
        patch("secrets.token_hex", return_value=_MINTED_TOKEN_CANARY),
    ):
        result = await _dispatch_rotate(rke2_e2e, approved=True)

    assert result["status"] == "ok", result.get("error")
    payload = result["result"]
    assert payload["rotated"] is True
    assert payload["token_ref"]["backend"] == "vault"
    assert payload["token_ref"]["kv_version"] == 5
    # Caller view carries no token value.
    assert _MINTED_TOKEN_CANARY not in json.dumps(result)

    # THE audit rule: the RAW audit-row payload (persisted pre-redaction) must
    # carry no token value either -- assert against the row, not just the view.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = await session.execute(
            select(AuditLog)
            .where(AuditLog.method == "DISPATCH", AuditLog.path == op_id)
            .order_by(AuditLog.occurred_at.desc())
            .limit(1)
        )
        row = rows.scalar_one()
    assert row.raw_payload is not None
    assert _MINTED_TOKEN_CANARY not in json.dumps(row.raw_payload)
    assert _MINTED_TOKEN_CANARY not in json.dumps(row.payload)
