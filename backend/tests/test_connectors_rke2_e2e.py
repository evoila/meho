# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RKE2 connector recorded-fixture / asyncssh fake-shell E2E test (#2221).

Drives ``rke2.about`` and ``rke2.posture.show`` through the full
``call_operation`` dispatch stack against an in-process asyncssh
fake-shell server that replays plain-SSH command stubs -- no Docker
dependency, no live RKE2 node. The shape mirrors the G3.8 Holodeck
recorded-fixture E2E precedent (``test_connectors_holodeck_e2e.py``).

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
