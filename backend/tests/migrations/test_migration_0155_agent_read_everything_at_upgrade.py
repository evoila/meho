# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0155_agent_read_everything_at_upgrade``.

The migration gives every existing agent one ``agent_permission`` row:
every operation, every target, ``auto-execute``, made by the marker
``meho:upgrade-read-everything``. The tests check:

* the permission answers do not change: for an agent with no rows, with
  one ``deny`` row and with target-scoped rows, every risk level, target
  and role gets the same verdict before and after the upgrade;
* who gets the row: live agents, other subs that already have a row, per
  tenant; not revoked agents, and not a sub that already has ``*``/``*``;
* the upgrade is safe to run twice, and the downgrade deletes only the
  marker rows;
* the grant list shows the row with the marker, and an admin can remove it.

Every step targets this migration's own revision (``0155``) and its
``down_revision`` (``0154``), never ``head``, so a later migration cannot
break these tests. The PostgreSQL twin is
``tests/integration/test_migration_0155_agent_read_everything_pg.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meho_backplane.agents.grants import AgentGrantService
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.permissions import resolve_verdict
from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AgentPermission, AgentPrincipal, Tenant
from meho_backplane.settings import get_settings

_REVISION = "0155"
_DOWN_REVISION = "0154"
_MARKER = "meho:upgrade-read-everything"
_MIGRATION_FILE = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0155_agent_read_everything_at_upgrade.py"
)

_TENANT = uuid.UUID("15500000-0000-0000-0000-000000000001")
_OTHER_TENANT = uuid.UUID("15500000-0000-0000-0000-000000000002")
_TARGET_ONE = uuid.UUID("15500000-0000-0000-0000-0000000000a1")
_TARGET_TWO = uuid.UUID("15500000-0000-0000-0000-0000000000a2")
_ADMIN = "admin:migration-0155"

# Agent principals (keycloak client ids) and their existing rows.
_NO_ROWS = "agent:no-rows"
_ONE_DENY = "agent:one-deny"
_TARGET_SCOPED = "agent:target-scoped"
_ALREADY_ALL = "agent:already-all"
_REVOKED = "agent:revoked"
_REVOKED_WITH_ROW = "agent:revoked-with-row"
_OTHER_TENANT_AGENT = "agent:other-tenant"
# A person's agent login: no agent principal row, only a grant row.
_USER_AGENT_SUB = "15500000-0000-0000-0000-0000000000b1"

_EXISTING_ROWS: list[tuple[uuid.UUID, str, str, str, str]] = [
    (_TENANT, _ONE_DENY, "vault.*", "*", "deny"),
    (_TENANT, _TARGET_SCOPED, "*", str(_TARGET_ONE), "deny"),
    (_TENANT, _TARGET_SCOPED, "vm.list", str(_TARGET_TWO), "needs-approval"),
    (_TENANT, _ALREADY_ALL, "*", "*", "deny"),
    (_TENANT, _REVOKED_WITH_ROW, "vm.list", "*", "auto-execute"),
    (_TENANT, _USER_AGENT_SUB, "vault.kv.read", "*", "needs-approval"),
]

_SAFETY_LEVELS = ("safe", "caution", "dangerous", "destructive")
_OPS = ("vault.kv.read", "vm.list", "GET:/api/things")
_TARGETS: tuple[uuid.UUID | None, ...] = (None, _TARGET_ONE, _TARGET_TWO)
_ROLES = (TenantRole.OPERATOR, TenantRole.READ_ONLY)

Verdicts = dict[tuple[str, str, str, str], str]


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + async URL."""
    db_path = tmp_path / "migration_0155.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, async_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_migration_0155", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _principal(tenant_id: uuid.UUID, client_id: str, *, revoked: bool = False) -> AgentPrincipal:
    name = client_id.removeprefix("agent:")
    return AgentPrincipal(
        tenant_id=tenant_id,
        name=name,
        keycloak_client_id=client_id,
        keycloak_internal_id=f"internal-{name}",
        owner_sub=_ADMIN,
        revoked=revoked,
        created_by_sub=_ADMIN,
    )


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            session.add_all(
                [
                    Tenant(id=_TENANT, slug="t-0155", name="Tenant 0155"),
                    Tenant(id=_OTHER_TENANT, slug="t-0155-b", name="Tenant 0155 B"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    _principal(_TENANT, _NO_ROWS),
                    _principal(_TENANT, _ONE_DENY),
                    _principal(_TENANT, _TARGET_SCOPED),
                    _principal(_TENANT, _ALREADY_ALL),
                    _principal(_TENANT, _REVOKED, revoked=True),
                    _principal(_TENANT, _REVOKED_WITH_ROW, revoked=True),
                    _principal(_OTHER_TENANT, _OTHER_TENANT_AGENT),
                ]
            )
            session.add_all(
                AgentPermission(
                    tenant_id=tenant_id,
                    principal_sub=sub,
                    op_pattern=op_pattern,
                    target_scope=target_scope,
                    verdict=verdict,
                    created_by_sub=_ADMIN,
                )
                for tenant_id, sub, op_pattern, target_scope, verdict in _EXISTING_ROWS
            )
            await session.commit()
    finally:
        await engine.dispose()


def _operators() -> list[tuple[str, Operator]]:
    """Agent logins as their tokens look: sub is a UUID, client id is the grant key."""
    logins: list[tuple[str, Operator]] = []
    for client_id in (_NO_ROWS, _ONE_DENY, _TARGET_SCOPED, _ALREADY_ALL):
        for role in _ROLES:
            logins.append(
                (
                    f"{client_id}/{role.value}",
                    Operator(
                        sub=str(uuid.uuid4()),
                        raw_jwt="<test-jwt>",
                        tenant_id=_TENANT,
                        tenant_role=role,
                        principal_kind=PrincipalKind.AGENT,
                        client_id=client_id,
                    ),
                )
            )
    logins.append(
        (
            "user-agent",
            Operator(
                sub=_USER_AGENT_SUB,
                raw_jwt="<test-jwt>",
                tenant_id=_TENANT,
                tenant_role=TenantRole.OPERATOR,
                principal_kind=PrincipalKind.AGENT,
            ),
        )
    )
    return logins


async def _verdict_matrix(url: str, operators: list[tuple[str, Operator]]) -> Verdicts:
    engine = create_async_engine(url)
    verdicts: Verdicts = {}
    try:
        async with async_sessionmaker(engine)() as session:
            for label, operator in operators:
                for op_id in _OPS:
                    for level in _SAFETY_LEVELS:
                        for target in _TARGETS:
                            verdict, _ = await resolve_verdict(
                                session=session,
                                operator=operator,
                                op_id=op_id,
                                safety_level=level,
                                target_id=target,
                            )
                            key = (label, op_id, level, str(target))
                            verdicts[key] = verdict.value
    finally:
        await engine.dispose()
    return verdicts


Row = tuple[str, str, str, str, str, str]


async def _read_rows(url: str) -> set[Row]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(
                    AgentPermission.tenant_id,
                    AgentPermission.principal_sub,
                    AgentPermission.op_pattern,
                    AgentPermission.target_scope,
                    AgentPermission.verdict,
                    AgentPermission.created_by_sub,
                )
            )
            return {
                (str(t), sub, pattern, scope, verdict, by)
                for t, sub, pattern, scope, verdict, by in result
            }
    finally:
        await engine.dispose()


def _rows(url: str) -> set[Row]:
    return asyncio.run(_read_rows(url))


async def _add_again(url: str) -> int:
    """Run the migration's insert step once more, outside Alembic."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            added: int = await conn.run_sync(_load_migration().add_upgrade_grants)
            return added
    finally:
        await engine.dispose()


def _marker_rows(url: str) -> set[tuple[str, str]]:
    return {
        (tenant_id, sub)
        for tenant_id, sub, op_pattern, target_scope, verdict, created_by in _rows(url)
        if created_by == _MARKER
        and (op_pattern, target_scope, verdict) == ("*", "*", "auto-execute")
    }


def _marker_subs(url: str) -> set[tuple[str, str]]:
    """The ``(tenant hex, sub)`` pairs that hold the upgrade row."""
    return {(uuid.UUID(tenant_id).hex, sub) for tenant_id, sub in _marker_rows(url)}


def _expected_marker_subs() -> set[tuple[str, str]]:
    """Live agents and other subs with rows; not revoked agents, not ``_ALREADY_ALL``."""
    tenant, other = _TENANT.hex, _OTHER_TENANT.hex
    return {
        (tenant, _NO_ROWS),
        (tenant, _ONE_DENY),
        (tenant, _TARGET_SCOPED),
        (tenant, _USER_AGENT_SUB),
        (other, _OTHER_TENANT_AGENT),
    }


def _prepared(cfg: Config, url: str, copy_migrated_db: Callable[[Config, str], None]) -> None:
    copy_migrated_db(cfg, _DOWN_REVISION)
    asyncio.run(_seed(url))


def test_answers_do_not_change(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, url = alembic_cfg
    _prepared(cfg, url, copy_migrated_db)
    operators = _operators()

    before = asyncio.run(_verdict_matrix(url, operators))
    command.upgrade(cfg, _REVISION)
    after = asyncio.run(_verdict_matrix(url, operators))

    assert before == after
    # The matrix must cover the cases the row could change.
    assert before[("agent:no-rows/operator", "vm.list", "safe", "None")] == "auto-execute"
    assert before[("agent:no-rows/operator", "vm.list", "caution", "None")] == "needs-approval"
    assert before[("agent:one-deny/operator", "vault.kv.read", "safe", "None")] == "deny"
    assert before[("agent:target-scoped/operator", "vm.list", "safe", str(_TARGET_ONE))] == "deny"
    assert before[("agent:already-all/operator", "vm.list", "safe", "None")] == "deny"


def test_who_gets_the_row(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, url = alembic_cfg
    _prepared(cfg, url, copy_migrated_db)

    command.upgrade(cfg, _REVISION)

    assert _marker_subs(url) == _expected_marker_subs()


def test_safe_to_run_twice_and_downgrade_removes_only_marker_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, url = alembic_cfg
    _prepared(cfg, url, copy_migrated_db)
    original = _rows(url)

    command.upgrade(cfg, _REVISION)
    once = _rows(url)

    assert asyncio.run(_add_again(url)) == 0
    assert _rows(url) == once

    command.downgrade(cfg, _DOWN_REVISION)
    assert _rows(url) == original

    command.upgrade(cfg, _REVISION)
    assert _rows(url) == once


def test_grant_list_shows_the_row_and_an_admin_can_remove_it(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, url = alembic_cfg
    _prepared(cfg, url, copy_migrated_db)
    command.upgrade(cfg, _REVISION)

    async def _list_then_revoke() -> tuple[list[str], bool, int]:
        service = AgentGrantService()
        grants = await service.list_(_TENANT, principal_sub=_NO_ROWS)
        removed = await service.revoke(_TENANT, grants[0].id)
        left = await service.list_(_TENANT, principal_sub=_NO_ROWS)
        return (
            [f"{g.op_pattern}|{g.target_scope}|{g.created_by_sub}" for g in grants],
            removed,
            len(left),
        )

    listed, removed, left = asyncio.run(_list_then_revoke())
    assert listed == [f"*|*|{_MARKER}"]
    assert removed is True
    assert left == 0
