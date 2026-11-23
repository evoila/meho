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
  read the secret when the connection was opened.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
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
from meho_backplane.connectors._shared.cache_key import credential_cache_key, target_cache_key
from meho_backplane.connectors._shared.vcf_auth import CredentialsCache
from meho_backplane.connectors.adapters import ssh as ssh_module
from meho_backplane.connectors.freeipa.connector import FreeipaConnector, _FreeipaSession
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.github.session import GitHubPATCredentials
from meho_backplane.connectors.linux.connector import LinuxSshConnector
from meho_backplane.connectors.phpipam.connector import PhpipamConnector
from meho_backplane.connectors.phpipam.session import PhpipamCredentials
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
        connector._pat_tokens[target_cache_key(target)] = "made-up-pat"
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
    warm=lambda c, t: c._cache.__setitem__(target_cache_key(t), dict(_BASIC)),
    call=lambda c, t, op: c.get(t, op),
    readers=lambda c: c.readers,
    cached=lambda c, t: target_cache_key(t) in c._cache,
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
