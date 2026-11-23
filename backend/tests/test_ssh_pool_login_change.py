# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""After a target's login changes, every SSH call signs in with the new login.

Runs the real SSH pool and the real dispatcher against an in-process SSH
server. The server accepts two made-up logins and tells each command which
login its connection signed in with, so every test reads the login from the
server's side. Only the Vault read is replaced: it maps a secret reference
to its made-up login and records who read which reference.

A call can hold the target as it was before a change: the dispatcher read
the row, then the change ran, then the call opens its SSH connection. The
tests hold such a call at a gate inside the operation, run the change the
way the target update route does (:func:`forget_target_in_connectors`), and
then let the held call go on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncssh
import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    forget_target_in_connectors,
    get_or_create_connector_instance,
)

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._targets_helpers import _empty_connector_registry, _settings_env  # noqa: F401

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_REF_A = f"tenants/{DEFAULT_TENANT_ID}/ssh-login-a"
_REF_B = f"tenants/{DEFAULT_TENANT_ID}/ssh-login-b"
_REF_C = f"tenants/{DEFAULT_TENANT_ID}/ssh-login-c"
#: Made-up logins of the stand-in server: secret reference -> (user, password).
_LOGINS = {
    _REF_A: ("login-a", "made-up-a"),
    _REF_B: ("login-b", "made-up-b"),
    _REF_C: ("login-c", "made-up-c"),
}
_USER_TO_REF = {user: ref for ref, (user, _pw) in _LOGINS.items()}
_PRODUCT = "sshloginchange"
_OP_ID = "sshloginchange.whoami"
_SERVER_KEY = asyncssh.generate_private_key("ssh-ed25519")


@dataclass(frozen=True)
class _Target:
    secret_ref: str
    host: str
    port: int
    connection_changed_at: datetime = _T0
    updated_at: datetime = _T0
    name: str = "ssh-login-change"
    auth_model: str | None = "shared_service_account"
    product: str = _PRODUCT
    version: str | None = None
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT


def _changed(target: _Target, secret_ref: str, minutes: int) -> _Target:
    """*target* after a person set another login on it."""
    at = _T0 + timedelta(minutes=minutes)
    return replace(target, secret_ref=secret_ref, connection_changed_at=at, updated_at=at)


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"{sub}.test.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _Server(asyncssh.SSHServer):
    """Accepts the made-up logins and records each sign-in."""

    signins: list[str]

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        ref = _USER_TO_REF.get(username)
        ok = ref is not None and _LOGINS[ref][1] == password
        if ok:
            self.signins.append(username)
        return ok


async def _whoami(process: Any) -> None:
    """Tell the command which login its connection signed in with."""
    process.stdout.write(process.get_extra_info("username"))
    process.exit(0)


class _LoginChangeSsh(SshConnector):
    product = _PRODUCT
    version = "1.x"
    impl_id = _PRODUCT
    fingerprint = probe = execute = None  # type: ignore[assignment]


@dataclass
class _Harness:
    host: str
    port: int
    signins: list[str]
    #: ``(caller sub, secret reference)`` for each Vault read.
    reads: list[tuple[str, str]]
    #: Gates the operation waits at, by name (``params["gate"]``).
    gates: dict[str, asyncio.Event]
    #: Set when a call has reached its gate, by name.
    at_gate: dict[str, asyncio.Event]
    #: When set, the Vault read waits for it (the change while logging in).
    read_gate: asyncio.Event | None = None
    read_started: asyncio.Event = field(default_factory=asyncio.Event)

    def target(self, secret_ref: str = _REF_A) -> _Target:
        return _Target(secret_ref=secret_ref, host=self.host, port=self.port)

    def gate(self, name: str) -> None:
        self.gates[name] = asyncio.Event()
        self.at_gate[name] = asyncio.Event()


#: The running harness; the operation handler must be a module-level function.
_HARNESS: list[_Harness] = []


async def _whoami_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    h = _HARNESS[0]
    name = params.get("gate")
    if name:
        h.at_gate[name].set()
        await h.gates[name].wait()
    connector = get_or_create_connector_instance(_LoginChangeSsh)
    assert isinstance(connector, _LoginChangeSsh)
    done = await connector._run_command(target, "whoami", operator=operator)
    return {"login": done.stdout}


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    signins: list[str] = []

    def _factory() -> _Server:
        server = _Server()
        server.signins = signins
        return server

    server = await asyncssh.create_server(
        _factory,
        "127.0.0.1",
        0,
        server_host_keys=[_SERVER_KEY],
        process_factory=_whoami,
    )
    port: int = server.sockets[0].getsockname()[1]
    h = _Harness(host="127.0.0.1", port=port, signins=signins, reads=[], gates={}, at_gate={})

    async def _resolve(self: SshConnector, target: Any, operator: Any = None) -> dict[str, Any]:
        del self
        h.read_started.set()
        if h.read_gate is not None:
            await h.read_gate.wait()
        h.reads.append((operator.sub, target.secret_ref))
        user, password = _LOGINS[target.secret_ref]
        return {"username": user, "password": password, "known_hosts_insecure": True}

    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_LoginChangeSsh)
    await register_typed_operation(
        product=_PRODUCT,
        version="1.x",
        impl_id=_PRODUCT,
        op_id=_OP_ID,
        handler=_whoami_handler,
        summary="Say which login the SSH connection uses.",
        description="Test op that runs one command over the pooled SSH connection.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )
    _HARNESS.append(h)
    with patch.object(SshConnector, "_resolve_secret", _resolve):
        yield h
    _HARNESS.clear()
    connector = get_or_create_connector_instance(_LoginChangeSsh)
    await connector.aclose()
    server.close()
    await server.wait_closed()


async def _call(target: _Target, operator: Operator, gate: str | None = None) -> Any:
    return await dispatch(
        operator=operator,
        connector_id=f"{_PRODUCT}-1.x",
        op_id=_OP_ID,
        target=target,
        params={"gate": gate} if gate else {},
    )


async def _login(target: _Target, operator: Operator) -> str:
    result = await _call(target, operator)
    assert result.status == "ok", result
    return str(result.result["login"])


async def _held_call(
    h: _Harness, target: _Target, operator: Operator, name: str
) -> asyncio.Task[Any]:
    """Start a call that has read *target* and waits at gate *name*."""
    h.gate(name)
    task = asyncio.create_task(_call(target, operator, gate=name))
    await asyncio.wait_for(h.at_gate[name].wait(), timeout=5)
    return task


async def _audit_sub(result: Any) -> str:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(AuditLog).where(AuditLog.id == result.audit_id))
        ).scalar_one()
    return row.operator_sub


@pytest.mark.asyncio
async def test_held_call_of_one_caller_does_not_give_the_old_login_to_later_calls(
    harness: _Harness,
) -> None:
    alice = _person("alice")
    old = harness.target(_REF_A)
    held = await _held_call(harness, old, alice, "held")

    new = _changed(old, _REF_B, minutes=5)
    await forget_target_in_connectors(new)
    harness.gates["held"].set()
    held_result = await held
    assert held_result.status == "ok", held_result
    assert held_result.result["login"] == "login-a"

    assert await _login(new, alice) == "login-b"
    assert await _login(new, alice) == "login-b"
    assert harness.signins == ["login-a", "login-b"]

    # Each audit row names the caller whose own Vault read gave the login used.
    assert await _audit_sub(held_result) == "alice"
    assert ("alice", _REF_A) in harness.reads
    assert ("alice", _REF_B) in harness.reads


@pytest.mark.asyncio
async def test_held_calls_of_two_callers_do_not_give_the_old_login_to_later_calls(
    harness: _Harness,
) -> None:
    alice, bob = _person("alice"), _person("bob")
    old = harness.target(_REF_A)
    held_alice = await _held_call(harness, old, alice, "alice")
    held_bob = await _held_call(harness, old, bob, "bob")

    new = _changed(old, _REF_B, minutes=5)
    await forget_target_in_connectors(new)
    harness.gates["alice"].set()
    harness.gates["bob"].set()
    results = await asyncio.gather(held_alice, held_bob)
    assert [r.result["login"] for r in results] == ["login-a", "login-a"]

    later = await asyncio.gather(_call(new, alice), _call(new, bob))
    assert [r.result["login"] for r in later] == ["login-b", "login-b"]
    assert sorted(harness.signins) == ["login-a", "login-a", "login-b", "login-b"]

    subs = [await _audit_sub(r) for r in (*results, *later)]
    assert subs == ["alice", "bob", "alice", "bob"]
    for sub in ("alice", "bob"):
        assert (sub, _REF_A) in harness.reads
        assert (sub, _REF_B) in harness.reads


@pytest.mark.asyncio
async def test_with_no_change_the_same_caller_reuses_the_connection(harness: _Harness) -> None:
    alice = _person("alice")
    target = harness.target(_REF_A)

    assert await _login(target, alice) == "login-a"
    assert await _login(target, alice) == "login-a"

    assert harness.signins == ["login-a"]
    assert harness.reads == [("alice", _REF_A)]


@pytest.mark.asyncio
async def test_a_change_back_to_the_first_login_reuses_no_connection_from_before(
    harness: _Harness,
) -> None:
    alice = _person("alice")
    first = harness.target(_REF_A)
    assert await _login(first, alice) == "login-a"
    held = await _held_call(harness, first, alice, "held")

    to_b = _changed(first, _REF_B, minutes=5)
    await forget_target_in_connectors(to_b)
    harness.gates["held"].set()
    assert (await held).result["login"] == "login-a"
    assert await _login(to_b, alice) == "login-b"

    back_to_a = _changed(first, _REF_A, minutes=10)
    await forget_target_in_connectors(back_to_a)
    assert await _login(back_to_a, alice) == "login-a"

    # The first login, the held call, the new login, and a fresh login after the change back.
    assert harness.signins == ["login-a", "login-a", "login-b", "login-a"]


@pytest.mark.asyncio
async def test_a_change_while_the_connection_opens_keeps_no_connection(
    harness: _Harness,
) -> None:
    alice = _person("alice")
    old = harness.target(_REF_A)
    harness.read_gate = asyncio.Event()
    opening = asyncio.create_task(_call(old, alice))
    await asyncio.wait_for(harness.read_started.wait(), timeout=5)

    new = _changed(old, _REF_B, minutes=5)
    await forget_target_in_connectors(new)
    harness.read_gate.set()
    result = await opening
    assert result.status == "error"
    assert result.error == "connector_error: SshTargetChangedError"

    harness.read_gate = None
    assert await _login(new, alice) == "login-b"
    assert harness.signins[-1] == "login-b"
    assert "login-a" not in harness.signins[1:]


@pytest.mark.asyncio
async def test_two_changes_in_a_row_leave_only_the_last_login(harness: _Harness) -> None:
    alice = _person("alice")
    old = harness.target(_REF_A)
    held = await _held_call(harness, old, alice, "held")

    to_b = _changed(old, _REF_B, minutes=5)
    await forget_target_in_connectors(to_b)
    to_c = _changed(old, _REF_C, minutes=10)
    await forget_target_in_connectors(to_c)
    harness.gates["held"].set()
    assert (await held).result["login"] == "login-a"

    assert await _login(to_c, alice) == "login-c"
    assert await _login(to_c, alice) == "login-c"
    assert harness.signins == ["login-a", "login-c"]


@pytest.mark.asyncio
async def test_a_target_deleted_while_a_call_runs_gives_its_login_to_no_new_target(
    harness: _Harness,
) -> None:
    alice = _person("alice")
    old = harness.target(_REF_A)
    held = await _held_call(harness, old, alice, "held")

    await forget_target_in_connectors(old)
    harness.gates["held"].set()
    assert (await held).result["login"] == "login-a"

    # A new target with the same name is a new row with another id.
    recreated = replace(harness.target(_REF_B), name=old.name)
    assert await _login(recreated, alice) == "login-b"
    assert harness.signins == ["login-a", "login-b"]
