# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that uses a login MEHO kept in memory names who read it.

A connector keeps a target's login after the first store read. A later
call of the same caller uses the kept entry and makes no store read. Its
audit row still names the login under which the secret was read:

* through the real dispatch, on the GitHub and the FreeIPA connector: one
  caller twice, two callers in turn, a kept entry whose reader is not
  known, and a call that uses no secret;
* for every connector cache: a second call of the same caller takes the
  kept path, makes no store read, and names the caller;
* phpIPAM: a refused store read after a failed login names no login, because
  the call used no kept secret;
* SSH: a call that reuses the caller's open connection names the login that
  read the secret when the connection was opened;
* a call that read a secret and then uses a kept entry with no known reader
  keeps the login of its read (every kind of kept login);
* Entra and Azure: a refused read of a changed secret reference names no
  login, because the call used no kept entry;
* Entra and Azure: a read of the first secret reference that ends after the
  reference changed does not count for the new one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.secret_read_as import (
    KEPT_LOGIN_READER_UNKNOWN,
    current_secret_read_as,
    record_caller_secret_read,
    track_secret_read_as,
)
from meho_backplane.connectors._shared.cache_key import (
    credential_cache_key,
    target_secret_cache_key,
)
from meho_backplane.connectors._shared.microsoft_login import (
    ClientSecretCredentials,
    MicrosoftAccessToken,
)
from meho_backplane.connectors._shared.vcf_auth import CredentialsCache
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.azure import connector as azure_module
from meho_backplane.connectors.entra import connector as entra_module
from meho_backplane.connectors.freeipa.connector import FreeipaConnector, _FreeipaSession
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.github.session import GitHubPATCredentials
from meho_backplane.connectors.linux.connector import LinuxSshConnector
from meho_backplane.connectors.phpipam.connector import PhpipamConnector
from meho_backplane.connectors.phpipam.session import PhpipamCredentials
from meho_backplane.connectors.proxmox.connector import ProxmoxConnector
from meho_backplane.connectors.proxmox.session import ProxmoxCredentials
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vault import VaultConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests.test_connector_caches_read_per_caller import _BASIC, CASES, _Case, _person
from tests.test_connector_caches_read_per_caller import _Target as _CacheTarget

_OP = "vault.demo.kept_login"
_NO_SECRET_OP = "vault.demo.no_secret"
_SSH_OP = "vault.demo.ssh_kept_connection"

#: The connector the demo handler calls (typed handlers must be module-level).
_CONNECTOR: dict[str, Any] = {}


@dataclass(frozen=True)
class _Target(_CacheTarget):
    """A connector target that the dispatcher resolves to the Vault connector."""

    product: str = "vault"
    preferred_impl_id: str | None = None
    fingerprint: Any = field(default_factory=lambda: SimpleNamespace(version="1.18.0"))


class _Store:
    """Stands in for the store read: notes the caller's login and counts reads."""

    def __init__(self, secret: Any) -> None:
        self._secret = secret
        self.reads: list[str] = []

    async def read(self, target: Any, operator: Operator) -> Any:
        del target
        self.reads.append(operator.sub)
        record_caller_secret_read(operator)
        return self._secret


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    register_connector_v2(product="vault", version="1.x", impl_id="vault", cls=VaultConnector)
    yield
    _CONNECTOR.clear()
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


async def _kept_login_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    await _CONNECTOR["connector"].auth_headers(target, operator)
    return {}


async def _ssh_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    await _CONNECTOR["connector"]._connect(target, operator)
    return {}


async def _no_secret_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    return {}


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _register(op_id: str, handler: Any, embedding: AsyncMock) -> None:
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        op_id=op_id,
        handler=handler,
        summary="Demo op for the kept-login tests.",
        description="Demo op used by the kept-login tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )


async def _rows(op_id: str) -> list[str | None]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.path == op_id)
                    .order_by(AuditLog.occurred_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    return [row.secret_read_as for row in rows]


def _github() -> tuple[GitHubRestConnector, _Store]:
    """A GitHub connector whose store read returns a token; it logs in nowhere."""
    store = _Store(GitHubPATCredentials(token="made-up-pat"))
    return GitHubRestConnector(credentials_loader=store.read), store


def _freeipa(target: _Target) -> tuple[FreeipaConnector, _Store]:
    """A FreeIPA connector holding a session for *target* that no caller has read yet."""
    store = _Store(_BASIC)
    connector = FreeipaConnector(credentials_loader=store.read)
    connector._sessions[credential_cache_key(target)] = _FreeipaSession(
        cookie="ipa_session=made-up", referer="https://made-up/ipa"
    )
    return connector, store


def _connector(name: str, target: _Target) -> tuple[Any, _Store]:
    return _github() if name == "github" else _freeipa(target)


async def _run_calls(
    connector: Any, store: _Store, target: _Target, subs: list[str], embedding: AsyncMock
) -> tuple[list[str | None], list[int]]:
    """Dispatch one call per sub; return the rows' values and the store reads per call."""
    _CONNECTOR["connector"] = connector
    await _register(_OP, _kept_login_handler, embedding)
    reads_per_call = []
    for sub in subs:
        before = len(store.reads)
        result = await dispatch(
            operator=_person(sub), connector_id="vault-1.x", op_id=_OP, target=target, params={}
        )
        assert result.status == "ok", result.error
        reads_per_call.append(len(store.reads) - before)
    return await _rows(_OP), reads_per_call


@pytest.mark.parametrize("name", ["github", "freeipa"])
@pytest.mark.asyncio
async def test_one_caller_twice_names_the_caller_both_times(
    name: str, embedding: AsyncMock
) -> None:
    target = _Target()
    connector, store = _connector(name, target)
    values, reads = await _run_calls(connector, store, target, ["alice", "alice"], embedding)
    assert values == ["alice", "alice"]
    assert reads == [1, 0]


@pytest.mark.parametrize("name", ["github", "freeipa"])
@pytest.mark.asyncio
async def test_two_callers_in_turn_each_name_only_themselves(
    name: str, embedding: AsyncMock
) -> None:
    target = _Target()
    connector, store = _connector(name, target)
    subs = ["alice", "carol", "carol", "alice"]
    values, reads = await _run_calls(connector, store, target, subs, embedding)
    assert values == subs
    assert reads == [1, 1, 0, 0]


@pytest.mark.parametrize("name", ["github", "freeipa"])
@pytest.mark.asyncio
async def test_a_kept_entry_with_no_known_reader_names_the_fixed_word(
    name: str, embedding: AsyncMock
) -> None:
    target = _Target()
    connector, store = _connector(name, target)
    if name == "github":
        connector._pat_tokens[target_secret_cache_key(target)] = "made-up-pat"
    # The reader is recorded outside a call, so the store read noted nothing.
    connector._readers.record(target, _person("alice"))
    values, reads = await _run_calls(connector, store, target, ["alice"], embedding)
    assert values == [KEPT_LOGIN_READER_UNKNOWN]
    assert reads == [0]


@pytest.mark.asyncio
async def test_a_call_that_uses_no_secret_leaves_it_empty(embedding: AsyncMock) -> None:
    await _register(_NO_SECRET_OP, _no_secret_handler, embedding)
    result = await dispatch(
        operator=_person("alice"),
        connector_id="vault-1.x",
        op_id=_NO_SECRET_OP,
        target=_Target(),
        params={},
    )
    assert result.status == "ok", result.error
    assert await _rows(_NO_SECRET_OP) == [None]


# ---------------------------------------------------------------------------
# Every connector cache
# ---------------------------------------------------------------------------


_VCF_CASE = _Case(
    name="vcf",
    secret=_BASIC,
    make=lambda loader: CredentialsCache(loader, product_label="vcf-demo"),
    warm=lambda c, t: c._cache.__setitem__(target_secret_cache_key(t), dict(_BASIC)),
    call=lambda c, t, op: c.get(t, op),
    readers=lambda c: c.readers,
    cached=lambda c, t: target_secret_cache_key(t) in c._cache,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [*CASES, _VCF_CASE], ids=str)
async def test_second_call_of_a_caller_names_the_caller_with_no_store_read(case: _Case) -> None:
    store = _Store(case.secret)
    connector = case.make(store.read)
    target = _Target()
    case.warm(connector, target)
    carol = _person("carol")

    with track_secret_read_as():
        await case.call(connector, target, carol)
        assert current_secret_read_as() == "carol"
    assert store.reads == ["carol"]

    with track_secret_read_as():
        await case.call(connector, target, carol)
        assert current_secret_read_as() == "carol"
    assert store.reads == ["carol"]


def test_the_fixed_word_cannot_be_taken_for_a_login_id() -> None:
    word = KEPT_LOGIN_READER_UNKNOWN
    assert not word.startswith(("agent:", "system:", "__"))
    with pytest.raises(ValueError):
        uuid.UUID(word)


# ---------------------------------------------------------------------------
# phpIPAM after a failed login, and the SSH connection pool
# ---------------------------------------------------------------------------


class _RefusedAfterFirstRead(_Store):
    """A store read that passes once and is refused after that."""

    async def read(self, target: Any, operator: Operator) -> Any:
        if self.reads:
            self.reads.append(operator.sub)
            raise PermissionError("made-up refusal")
        return await super().read(target, operator)


@pytest.mark.asyncio
async def test_phpipam_refused_read_after_a_failed_login_names_no_login(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    store = _RefusedAfterFirstRead(PhpipamCredentials(username="svc", password="made-up-password"))
    connector = PhpipamConnector(credentials_loader=store.read)
    monkeypatch.setattr(connector, "_login", AsyncMock(side_effect=RuntimeError("made-up")))
    _CONNECTOR["connector"] = connector
    await _register(_OP, _kept_login_handler, embedding)
    target = _Target()
    for _ in range(2):
        result = await dispatch(
            operator=_person("alice"), connector_id="vault-1.x", op_id=_OP, target=target, params={}
        )
        assert result.status != "ok"
    # The first call's store read passed; the second call's was refused and
    # it used no kept secret.
    assert await _rows(_OP) == ["alice", None]
    assert store.reads == ["alice", "alice"]


@pytest.mark.asyncio
async def test_ssh_reuse_of_an_open_connection_names_the_reader(
    monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    store = _Store({"username": "svc", "password": "made-up", "known_hosts_insecure": True})
    logins: list[MagicMock] = []

    async def _dial(target: Any, auth_kwargs: dict[str, Any]) -> MagicMock:
        login = MagicMock()
        login.is_closed.return_value = False
        logins.append(login)
        return login

    monkeypatch.setattr(ssh_module, "load_vault_secret_data", store.read)
    monkeypatch.setattr(ssh_module, "_dial", _dial)
    _CONNECTOR["connector"] = LinuxSshConnector()
    await _register(_SSH_OP, _ssh_handler, embedding)
    target = _Target()
    subs = ["alice", "alice", "carol", "carol", "alice"]
    for sub in subs:
        result = await dispatch(
            operator=_person(sub), connector_id="vault-1.x", op_id=_SSH_OP, target=target, params={}
        )
        assert result.status == "ok", result.error
    assert await _rows(_SSH_OP) == subs
    assert store.reads == ["alice", "carol"]
    assert len(logins) == 2


# ---------------------------------------------------------------------------
# A kept login is noted only from what happened in this call
# ---------------------------------------------------------------------------

_READ_THEN_KEPT_OP = "vault.demo.read_then_kept_login"
_GUID = "00000000-0000-0000-0000-00000000aaaa"
_MICROSOFT_HOSTS = {"entra": "graph.microsoft.com", "azure": "management.azure.com"}


async def _use_login(operator: Operator, target: Any) -> None:
    """Use the target's login the way the connector under test does."""
    connector = _CONNECTOR["connector"]
    if isinstance(connector, LinuxSshConnector):
        await connector._connect(target, operator)
    else:
        await connector.auth_headers(target, operator)


async def _use_login_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    await _use_login(operator, target)
    return {}


async def _read_then_kept_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    await _CONNECTOR["first_store"].read(target, operator)
    await _use_login(operator, target)
    return {}


def _kept_login_connector(
    name: str, monkeypatch: pytest.MonkeyPatch, store_cls: type[_Store] = _Store
) -> tuple[Any, _Store, _Target]:
    """A connector of kind *name*, its store and a target; it logs in nowhere."""
    if name in _MICROSOFT_HOSTS:
        module = {"entra": entra_module, "azure": azure_module}[name]
        cls: Any = getattr(module, f"{name.capitalize()}Connector")
        token = MicrosoftAccessToken(
            value="made-up-token", expires_in=3600, app_id=None, object_id=None, tenant_id=None
        )
        monkeypatch.setattr(module, "fetch_client_credentials_token", AsyncMock(return_value=token))
        store = store_cls(
            ClientSecretCredentials(tenant_id=_GUID, client_id=_GUID, client_secret="made-up")
        )
        target = _Target(host=_MICROSOFT_HOSTS[name], extras={"subscription_id": _GUID})
        return cls(credentials_loader=store.read), store, target
    if name == "ssh":
        store = store_cls({"username": "svc", "password": "made-up", "known_hosts_insecure": True})
        login = MagicMock()
        login.is_closed.return_value = False
        monkeypatch.setattr(ssh_module, "load_vault_secret_data", store.read)
        monkeypatch.setattr(ssh_module, "_dial", AsyncMock(return_value=login))
        return LinuxSshConnector(), store, _Target()
    if name == "github":
        connector, github_store = _github()
        return connector, github_store, _Target()
    store = store_cls(
        ProxmoxCredentials(mode="token", token_id="svc@pam!meho", token_secret="made-up")
    )
    return ProxmoxConnector(credentials_loader=store.read), store, _Target()


async def _call(op_id: str, target: _Target, sub: str = "alice") -> str:
    result = await dispatch(
        operator=_person(sub), connector_id="vault-1.x", op_id=op_id, target=target, params={}
    )
    return result.status


@pytest.mark.parametrize("name", ["proxmox", "github", "entra", "azure", "ssh"])
@pytest.mark.asyncio
async def test_a_known_login_stays_when_a_kept_entry_has_no_known_reader(
    name: str, monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    connector, store, target = _kept_login_connector(name, monkeypatch)
    first_store = _Store({"made-up": "value"})
    _CONNECTOR.update(connector=connector, first_store=first_store)
    await _register(_OP, _use_login_handler, embedding)
    await _register(_READ_THEN_KEPT_OP, _read_then_kept_handler, embedding)
    # This read runs outside a call, so the kept entry has no known reader.
    await _use_login(_person("alice"), target)
    assert store.reads == ["alice"]

    assert await _call(_READ_THEN_KEPT_OP, target) == "ok"
    assert await _rows(_READ_THEN_KEPT_OP) == ["alice"]
    assert first_store.reads == ["alice"]

    # The same kept entry in a call that has noted no login.
    assert await _call(_OP, target) == "ok"
    assert await _rows(_OP) == [KEPT_LOGIN_READER_UNKNOWN]
    assert store.reads == ["alice"]


class _RefusesOtherReference(_Store):
    """A store that refuses every secret reference but the first target's."""

    async def read(self, target: Any, operator: Operator) -> Any:
        if target.secret_ref != _Target().secret_ref:
            self.reads.append(operator.sub)
            raise PermissionError("made-up refusal")
        return await super().read(target, operator)


@pytest.mark.parametrize("name", list(_MICROSOFT_HOSTS))
@pytest.mark.asyncio
async def test_a_refused_read_of_a_changed_secret_reference_names_no_login(
    name: str, monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    connector, store, target = _kept_login_connector(name, monkeypatch, _RefusesOtherReference)
    _CONNECTOR["connector"] = connector
    await _register(_OP, _use_login_handler, embedding)
    changed = replace(target, secret_ref="tenants/test/made-up-other")
    statuses, reads = [], []
    for used in (target, target, changed, target):
        before = len(store.reads)
        statuses.append(await _call(_OP, used) == "ok")
        reads.append(len(store.reads) - before)
    # No kept entry: the read. A kept entry: its reader. The changed reference:
    # no kept entry and a refused read. The first reference: its kept entry.
    assert await _rows(_OP) == ["alice", "alice", None, "alice"]
    assert statuses == [True, True, False, True]
    assert reads == [1, 0, 1, 0]


class _SlowStoreRefusesBob(_Store):
    """A store read that lets the other calls run first, and refuses bob."""

    async def read(self, target: Any, operator: Operator) -> Any:
        await asyncio.sleep(0.01)
        if operator.sub == "bob":
            self.reads.append(operator.sub)
            raise PermissionError("made-up refusal")
        return await super().read(target, operator)


@pytest.mark.parametrize("name", list(_MICROSOFT_HOSTS))
@pytest.mark.asyncio
async def test_calls_that_wait_for_the_first_read_name_only_their_own_login(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector, store, target = _kept_login_connector(name, monkeypatch, _SlowStoreRefusesBob)

    async def one(sub: str) -> str | None:
        with track_secret_read_as():
            try:
                await connector.auth_headers(target, _person(sub))
            except PermissionError:
                assert sub == "bob"
            return current_secret_read_as()

    noted = await asyncio.gather(one("alice"), one("bob"), one("carol"), one("alice"))
    assert noted == ["alice", None, "carol", "alice"]
    # The second call of alice waited for her first read and took the kept entry.
    assert store.reads == ["alice", "bob", "carol"]


_OTHER_CALLER_OP = "vault.demo.kept_login_other_caller"


class _SlowFirstReference(_RefusesOtherReference):
    """Reads the first reference only when told to; refuses alice the other one."""

    def __init__(self, secret: Any) -> None:
        super().__init__(secret)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, target: Any, operator: Operator) -> Any:
        if operator.sub != "alice":
            return await _Store.read(self, target, operator)
        if target.secret_ref == _Target().secret_ref:
            self.started.set()
            await self.release.wait()
        return await super().read(target, operator)


@pytest.mark.parametrize("name", list(_MICROSOFT_HOSTS))
@pytest.mark.asyncio
async def test_a_read_that_ends_after_the_reference_changed_does_not_count_for_the_new_one(
    name: str, monkeypatch: pytest.MonkeyPatch, embedding: AsyncMock
) -> None:
    connector, store, target = _kept_login_connector(name, monkeypatch, _SlowFirstReference)
    assert isinstance(store, _SlowFirstReference)
    login = {"entra": entra_module, "azure": azure_module}[name].fetch_client_credentials_token
    assert isinstance(login, AsyncMock)
    _CONNECTOR["connector"] = connector
    await _register(_OP, _use_login_handler, embedding)
    await _register(_OTHER_CALLER_OP, _use_login_handler, embedding)
    changed = replace(target, secret_ref="tenants/test/made-up-other")

    first_call = asyncio.create_task(_call(_OP, target))
    await asyncio.wait_for(store.started.wait(), timeout=10)
    # The target changes while alice's read runs, and carol loads the new secret.
    await connector.invalidate_credentials(target)
    assert await _call(_OTHER_CALLER_OP, changed, "carol") == "ok"
    store.release.set()
    assert await first_call == "ok"
    # Alice's call logged in with the secret she read, not with carol's token.
    assert login.await_count == 2

    # The store is asked for alice's read of the new secret, and refuses it.
    assert await _call(_OP, changed) != "ok"
    assert store.reads == ["carol", "alice", "alice"]
    assert await _rows(_OP) == ["alice", None]

    # Carol's kept entry stays in use.
    assert await _call(_OTHER_CALLER_OP, changed, "carol") == "ok"
    assert await _rows(_OTHER_CALLER_OP) == ["carol", "carol"]
    assert store.reads == ["carol", "alice", "alice"]
    assert login.await_count == 2
