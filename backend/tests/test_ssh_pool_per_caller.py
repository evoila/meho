# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The SSH connection pool keeps one login per caller and target.

Runs the real :class:`LinuxSshConnector`. Only two seams are replaced: the
SSH login (``asyncssh.connect``) and the Vault read
(``load_vault_secret_data``). The Vault stand-in allows the read for named
people only, so the tests show that every caller gets a connection only
after their own Vault read passed, also when another caller's connection
to the same target is already open.

The check runner runs a Sensor under the ``sub`` of the person who made it,
with the runner's own token and Vault role (``check_runner_dispatch``). So
the runner stand-in here carries a person's ``sub``, and the Vault stand-in
tells the runner apart by that marker, not by the ``sub``.

A runbook verification runs under the assigned person's ``sub`` with no
token (``raw_jwt=""``). The real Vault backend refuses such a read before
any network call; the stand-in here does the same.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultRoleDeniedError
from meho_backplane.connectors._shared.system_operator import SYSTEM_OPERATOR_SUB
from meho_backplane.connectors._shared.vault_creds import OperatorTokenRequiredError
from meho_backplane.connectors.adapters.ssh import _pool_key
from meho_backplane.connectors.linux.connector import LinuxSshConnector
from tests._ssh_fakes import FakeSshProcess

_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SECRET_REF = "tenants/test/linux-host"
_SECRET = {"username": "svc", "password": "made-up-password", "known_hosts_insecure": True}


@dataclass(frozen=True)
class _Target:
    name: str = "linux-host"
    host: str = "host.example.invalid"
    port: int | None = 22
    secret_ref: str | None = _SECRET_REF
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT_ID


def _person(sub: str, role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"{sub}.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=role,
    )


def _check_runner(sub: str) -> Operator:
    """The identity the Sensor runner dispatches with.

    Shaped like ``checks.runner._sensor_operator``: the ``sub`` of the
    person who made the Sensor, the runner's own token, and the
    ``check_runner_dispatch`` marker that picks the runner's Vault role.
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="runner.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        check_runner_dispatch=True,
    )


def _runbook_verification(sub: str) -> Operator:
    """The identity a runbook verification dispatches with.

    Shaped like ``runbooks.run_service._build_operator_for_dispatch``: the
    ``sub`` of the assigned person and no token at all.
    """
    return Operator(
        sub=sub,
        name=sub,
        email=None,
        raw_jwt="",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _reader(operator: Operator) -> str:
    """Name the Vault reader as the stand-in records it.

    ``runner:<sub>`` for the check runner, ``no-token:<sub>`` for a call
    without a token, else the ``sub``.
    """
    if not operator.raw_jwt:
        return f"no-token:{operator.sub}"
    return f"runner:{operator.sub}" if operator.check_runner_dispatch else operator.sub


class _FakeLogin:
    """Stands in for an open ``asyncssh.SSHClientConnection``."""

    def __init__(self, sub: str) -> None:
        self.sub = sub
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def create_process(self, cmd: str, **_kwargs: Any) -> FakeSshProcess:
        return FakeSshProcess(command=cmd, stdout="ok\n")


class _Harness:
    """Counts Vault reads and SSH logins per caller.

    ``allowed`` names the readers whose Vault read passes, as
    :func:`_reader` spells them: a person by ``sub``, the check runner
    as ``runner:<sub>``. The runner's role is refused unless it is named,
    whatever ``sub`` the Sensor carries.
    """

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.vault_reads: list[str] = []
        self.logins: list[_FakeLogin] = []

    async def read_secret(self, target: Any, operator: Operator, **_kwargs: Any) -> dict[str, Any]:
        del target
        reader = _reader(operator)
        self.vault_reads.append(reader)
        if not operator.raw_jwt:
            raise OperatorTokenRequiredError(f"read refused for {reader}: no token")
        if reader not in self.allowed:
            raise VaultRoleDeniedError(f"read refused for {reader}")
        return dict(_SECRET)

    async def connect(self, *_args: Any, **kwargs: Any) -> _FakeLogin:
        login = _FakeLogin(kwargs["username"])
        self.logins.append(login)
        return login


@pytest.fixture
def harness() -> Any:
    h = _Harness(allowed={"alice", "carol"})
    with (
        patch("meho_backplane.connectors.adapters.ssh.load_vault_secret_data", h.read_secret),
        patch("asyncssh.connect", h.connect),
    ):
        yield h


async def _run(conn: LinuxSshConnector, target: _Target, operator: Operator | None) -> Any:
    return await conn._run_command(target, "true", operator=operator)  # type: ignore[arg-type]


def _refused_callers() -> list[Operator | None]:
    """A person without access, a Sensor of a person with access, and no operator."""
    return [_person("bob"), _check_runner("alice"), None]


@pytest.mark.asyncio
async def test_cold_pool_refuses_each_caller_whose_vault_read_is_refused(harness: Any) -> None:
    conn = LinuxSshConnector()
    target = _Target()
    for caller in _refused_callers():
        with pytest.raises(VaultRoleDeniedError):
            await _run(conn, target, caller)
    assert harness.vault_reads == ["bob", "runner:alice", SYSTEM_OPERATOR_SUB]
    assert harness.logins == []
    assert conn._connections == {}


@pytest.mark.asyncio
async def test_warm_pool_still_asks_vault_for_each_new_caller(harness: Any) -> None:
    """Another caller's open connection to the target is never handed out."""
    conn = LinuxSshConnector()
    target = _Target()
    alice = _person("alice", TenantRole.TENANT_ADMIN)

    await _run(conn, target, alice)
    assert harness.vault_reads == ["alice"]
    assert len(harness.logins) == 1

    for caller in _refused_callers():
        with pytest.raises(VaultRoleDeniedError):
            await _run(conn, target, caller)

    assert harness.vault_reads == ["alice", "bob", "runner:alice", SYSTEM_OPERATOR_SUB]
    assert len(harness.logins) == 1
    assert list(conn._connections) == [_pool_key(target, alice)]
    assert not harness.logins[0].closed


@pytest.mark.asyncio
async def test_sensor_of_a_person_never_reuses_their_login(harness: Any) -> None:
    """The check runner runs a Sensor under its creator's ``sub``.

    The runner's own Vault role is refused here. After the person opened a
    connection, the Sensor with the same ``sub`` must still run the
    runner's Vault read, be refused, and get no connection.
    """
    conn = LinuxSshConnector()
    target = _Target()
    alice, sensor_of_alice = _person("alice"), _check_runner("alice")

    await _run(conn, target, alice)
    assert len(harness.logins) == 1

    with pytest.raises(VaultRoleDeniedError):
        await _run(conn, target, sensor_of_alice)

    assert harness.vault_reads == ["alice", "runner:alice"]
    assert len(harness.logins) == 1
    assert _pool_key(target, sensor_of_alice) != _pool_key(target, alice)
    assert list(conn._connections) == [_pool_key(target, alice)]

    # The other way round too: the person never gets the Sensor's login.
    harness.allowed.add("runner:carol")
    carol, sensor_of_carol = _person("carol"), _check_runner("carol")
    await _run(conn, target, sensor_of_carol)
    await _run(conn, target, carol)
    await _run(conn, target, sensor_of_carol)
    await _run(conn, target, carol)

    assert harness.vault_reads == ["alice", "runner:alice", "runner:carol", "carol"]
    assert len(harness.logins) == 3
    assert conn._connections[_pool_key(target, sensor_of_carol)][0] is harness.logins[1]
    assert conn._connections[_pool_key(target, carol)][0] is harness.logins[2]


@pytest.mark.asyncio
async def test_runbook_verification_without_token_never_reuses_a_login(harness: Any) -> None:
    """A runbook verification runs under the assigned person's ``sub`` with no token.

    After the person opened a connection, the verification with the same
    ``sub`` must still run its own Vault read, be refused for the missing
    token, and get no connection. A connection of the person's Sensor is
    out of its reach too.
    """
    conn = LinuxSshConnector()
    target = _Target()
    alice, verification_as_alice = _person("alice"), _runbook_verification("alice")

    await _run(conn, target, alice)
    assert len(harness.logins) == 1

    with pytest.raises(OperatorTokenRequiredError):
        await _run(conn, target, verification_as_alice)

    assert harness.vault_reads == ["alice", "no-token:alice"]
    assert len(harness.logins) == 1
    assert _pool_key(target, verification_as_alice) != _pool_key(target, alice)
    assert list(conn._connections) == [_pool_key(target, alice)]
    assert not harness.logins[0].closed

    harness.allowed.add("runner:alice")
    await _run(conn, target, _check_runner("alice"))
    assert len(harness.logins) == 2

    with pytest.raises(OperatorTokenRequiredError):
        await _run(conn, target, verification_as_alice)

    assert harness.vault_reads == ["alice", "no-token:alice", "runner:alice", "no-token:alice"]
    assert len(harness.logins) == 2
    assert all(not login.closed for login in harness.logins)


@pytest.mark.asyncio
async def test_same_caller_reuses_their_own_login(harness: Any) -> None:
    conn = LinuxSshConnector()
    target = _Target()
    alice = _person("alice")

    await _run(conn, target, alice)
    await _run(conn, target, alice)

    assert harness.vault_reads == ["alice"]
    assert len(harness.logins) == 1


@pytest.mark.asyncio
async def test_two_allowed_callers_each_log_in_once(harness: Any) -> None:
    conn = LinuxSshConnector()
    target = _Target()
    alice, carol = _person("alice"), _person("carol")

    await _run(conn, target, alice)
    await _run(conn, target, carol)
    await _run(conn, target, alice)
    await _run(conn, target, carol)

    assert harness.vault_reads == ["alice", "carol"]
    assert len(harness.logins) == 2
    assert set(conn._connections) == {_pool_key(target, alice), _pool_key(target, carol)}
    assert conn._connections[_pool_key(target, alice)][0] is harness.logins[0]
    assert conn._connections[_pool_key(target, carol)][0] is harness.logins[1]


@pytest.mark.asyncio
async def test_target_change_drops_every_login_of_that_target_only(harness: Any) -> None:
    conn = LinuxSshConnector()
    changed, other = _Target(), _Target(name="other-host")
    alice, carol = _person("alice"), _person("carol")

    await _run(conn, changed, alice)
    await _run(conn, changed, carol)
    await _run(conn, other, alice)
    assert len(harness.logins) == 3

    await conn.invalidate_credentials(changed)  # type: ignore[arg-type]

    assert harness.logins[0].closed
    assert harness.logins[1].closed
    assert not harness.logins[2].closed
    assert list(conn._connections) == [_pool_key(other, alice)]

    # Each caller logs in again, after their own Vault read.
    await _run(conn, changed, alice)
    await _run(conn, changed, carol)
    assert harness.vault_reads == ["alice", "carol", "alice", "alice", "carol"]
    assert len(harness.logins) == 5


@pytest.mark.asyncio
async def test_aclose_closes_every_caller_login(harness: Any) -> None:
    conn = LinuxSshConnector()
    target = _Target()
    await _run(conn, target, _person("alice"))
    await _run(conn, target, _person("carol"))

    await conn.aclose()

    assert all(login.closed for login in harness.logins)
    assert conn._connections == {}
