# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0178_add_people_behind_columns``.

The migration adds four nullable columns: ``approval_request.people_behind``,
``agent_definition.updated_by_sub``, ``runbook_runs.reassigned_by`` and
``run_approval.principal_client_id``, and the table ``machine_login``. It
fills ``machine_login`` from the requests of machine logins and from the
``client`` block of the audit rows. It fills ``updated_by_sub`` from the
newest successful ``agent.edit`` audit row of the definition by a login that
is not a machine login, else from ``created_by_sub``. A machine login is
also the login of an agent, an add-on pairing or a runner of the tenant,
when none of its records names it as one. An edit from before the
definition was created (an edit of a deleted definition with the same name)
does not count. ``downgrade()`` removes the columns and the table again.
Every step targets this migration's own revision (``0178``) and its
``down_revision`` (``0177``), never ``head``.

The PostgreSQL twin is
``tests/integration/test_migration_0178_people_behind_pg.py``; it uses the
seed helpers of this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0178"
_DOWN_REVISION = "0177"
_NEW = {
    "approval_request": "people_behind",
    "agent_definition": "updated_by_sub",
    "runbook_runs": "reassigned_by",
    "run_approval": "principal_client_id",
}
_TENANT = "00000000-0000-0000-0000-000000000175"
_OTHER_TENANT = "00000000-0000-0000-0000-000000000176"
_EDITED = "00000000-0000-0000-0000-0000000001e1"
_NEVER_EDITED = "00000000-0000-0000-0000-0000000001e2"
_RECREATED = "00000000-0000-0000-0000-0000000001e3"

#: The definitions to seed: (id, tenant, name, created_by_sub, created_at).
#: "helper" was deleted and made again on 2026-11-10; its only edit is older.
DEFINITIONS = (
    (_EDITED, _TENANT, "triage", "creator-a", "2026-10-01 00:00:00"),
    (_NEVER_EDITED, _TENANT, "nightly", "creator-b", "2026-10-01 00:00:00"),
    (_RECREATED, _TENANT, "helper", "creator-c", "2026-11-10 00:00:00"),
)

#: The audit rows to seed: (operator_sub, tenant, op_id, agent_name, status, occurred_at).
#: The newest successful edit of "triage" in its tenant by a person is by
#: "editor-new". A failed edit, an edit in another tenant, an edit of another
#: agent and the edits of two machine logins are newer and do not count: the
#: audit row of "agent-editor" names an agent login, and "agent:x" is the
#: login of the seeded definitions.
AUDIT_ROWS = (
    ("editor-old", _TENANT, "agent.edit", "triage", 200, "2026-11-01 10:00:00"),
    ("editor-new", _TENANT, "agent.edit", "triage", 200, "2026-11-02 10:00:00"),
    ("editor-failed", _TENANT, "agent.edit", "triage", 422, "2026-11-03 10:00:00"),
    ("editor-elsewhere", _OTHER_TENANT, "agent.edit", "triage", 200, "2026-11-04 10:00:00"),
    ("editor-of-other", _TENANT, "agent.edit", "other", 200, "2026-11-05 10:00:00"),
    ("creator-a", _TENANT, "agent.create", "triage", 201, "2026-11-06 10:00:00"),
    ("editor-of-deleted", _TENANT, "agent.edit", "helper", 200, "2026-11-07 10:00:00"),
    ("agent-editor", _TENANT, "agent.edit", "triage", 200, "2026-11-08 10:00:00"),
    ("agent:x", _TENANT, "agent.edit", "nightly", 200, "2026-11-09 10:00:00"),
)

#: The audit row of these logins carries a ``client`` block that names an agent.
MACHINE_EDITORS = frozenset({"agent-editor"})

#: What the fill gives each definition.
EXPECTED = [
    (_EDITED, "editor-new"),
    (_NEVER_EDITED, "creator-b"),
    (_RECREATED, "creator-c"),
]


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0178.db"
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
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _columns(sync_url: str, table: str) -> dict[str, bool]:
    engine = sa_create_engine(sync_url)
    try:
        return {c["name"]: c["nullable"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _tables(sync_url: str) -> list[str]:
    engine = sa_create_engine(sync_url)
    try:
        return inspect(engine).get_table_names()
    finally:
        engine.dispose()


def _run(sync_url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


#: Insert statements shared with the PostgreSQL twin.
DEFINITION_SQL = (
    "INSERT INTO agent_definition (id, tenant_id, name, identity_ref, model_tier, "
    "system_prompt, toolset, turn_budget, enabled, created_by_sub, created_at, updated_at) "
    "VALUES (:id, :tenant, :name, 'agent:x', 'small', 'p', '{}', 5, true, :creator, :at, :at)"
)
AUDIT_SQL = (
    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, payload, "
    "tenant_id, event_class, sandbox_mode) VALUES (:id, :at, :sub, 'PATCH', '/api/v1/agents/x', "
    ":status, :payload, :tenant, 'legacy_request', false)"
)


def audit_payload(op_id: str, agent_name: str, sub: str) -> str:
    """The JSON payload of one seeded audit row."""
    payload: dict[str, object] = {"op_id": op_id, "agent_name": agent_name}
    if sub in MACHINE_EDITORS:
        payload["client"] = {"principal_kind": "agent", "channel": "token"}
    return json.dumps(payload)


def test_up_adds_four_nullable_columns_and_a_table_and_down_drops_them(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    for table, column in _NEW.items():
        assert column not in _columns(sync_url, table)

    command.upgrade(cfg, _REVISION)
    for table, column in _NEW.items():
        assert _columns(sync_url, table)[column] is True
    assert set(_columns(sync_url, "machine_login")) == {"tenant_id", "sub", "kind", "first_seen_at"}

    command.downgrade(cfg, _DOWN_REVISION)
    for table, column in _NEW.items():
        assert column not in _columns(sync_url, table)
    assert "machine_login" not in _tables(sync_url)


def test_updated_by_sub_comes_from_the_newest_edit_or_the_creator(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    for definition_id, tenant, name, creator, created_at in DEFINITIONS:
        _run(
            sync_url,
            DEFINITION_SQL,
            id=definition_id,
            tenant=tenant,
            name=name,
            creator=creator,
            at=created_at,
        )
    for index, (sub, tenant, op_id, agent_name, status, at) in enumerate(AUDIT_ROWS):
        _run(
            sync_url,
            AUDIT_SQL,
            id=f"00000000-0000-0000-0000-{index:012d}",
            at=at,
            sub=sub,
            status=status,
            payload=audit_payload(op_id, agent_name, sub),
            tenant=tenant,
        )

    command.upgrade(cfg, _REVISION)

    rows = _run(sync_url, "SELECT id, updated_by_sub FROM agent_definition ORDER BY id")
    assert rows == EXPECTED


def test_machine_login_comes_from_machine_requests_and_audit_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    for index, (sub, kind) in enumerate(
        (("service-sender", "service"), ("person-sender", "user"), ("person-old", None))
    ):
        _run(
            sync_url,
            "INSERT INTO approval_request (id, tenant_id, principal_sub, principal_kind, op_id, "
            "connector_id, params_hash, proposed_effect, status, created_at) VALUES (:id, "
            ":tenant, :sub, :kind, 'x.write', 'x-1.0', 'h', '{}', 'pending', :at)",
            id=f"00000000-0000-0000-0000-0000000002{index:02d}",
            tenant=_TENANT,
            sub=sub,
            kind=kind,
            at="2026-11-01 10:00:00",
        )
    for index, (sub, tenant, op_id, agent_name, status, at) in enumerate(AUDIT_ROWS):
        _run(
            sync_url,
            AUDIT_SQL,
            id=f"00000000-0000-0000-0000-{index:012d}",
            at=at,
            sub=sub,
            status=status,
            payload=audit_payload(op_id, agent_name, sub),
            tenant=tenant,
        )

    command.upgrade(cfg, _REVISION)

    rows = _run(sync_url, "SELECT sub, kind FROM machine_login ORDER BY sub")
    assert rows == [("agent-editor", "agent"), ("service-sender", "service")]


#: The machine logins of the tenant that only the records of an add-on
#: pairing, an agent or a runner name. None of them made a request and no
#: audit row of theirs has a ``client`` block.
_PAIRED_DEFINITION = "00000000-0000-0000-0000-0000000001e4"
_PAIRING_SQL = (
    "INSERT INTO addon_pairing (id, tenant_id, name, keycloak_client_id, keycloak_internal_id, "
    "service_account_sub, owner_sub, contract_version, addon_contract_version, "
    "addon_min_backplane_version, created_by_sub, paired_at, updated_at) VALUES (:id, :tenant, "
    ":name, :client, 'kc-internal', :service_sub, 'owner', 1, 1, 1, 'owner', :at, :at)"
)
_PRINCIPAL_SQL = (
    "INSERT INTO {table} (id, tenant_id, name, keycloak_client_id, keycloak_internal_id, "
    "owner_sub, created_by_sub, created_at, updated_at) VALUES (:id, :tenant, :name, :client, "
    "'kc-internal', 'owner', 'owner', :at, :at)"
)
#: The edits of the definition, oldest first. Only the first is by a person.
_PAIRED_EDITS = (
    "editor-person",
    "pairing-service-sub",
    "pairing-client",
    "agent-client",
    "runner-client",
)


def test_updated_by_sub_skips_paired_agent_and_runner_logins(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    created = "2026-10-01 00:00:00"
    _run(
        sync_url,
        DEFINITION_SQL,
        id=_PAIRED_DEFINITION,
        tenant=_TENANT,
        name="paired",
        creator="creator-d",
        at=created,
    )
    _run(
        sync_url,
        _PAIRING_SQL,
        id="00000000-0000-0000-0000-0000000003a1",
        tenant=_TENANT,
        name="addon",
        client="pairing-client",
        service_sub="pairing-service-sub",
        at=created,
    )
    # A pairing without a service login must not hide every editor.
    _run(
        sync_url,
        _PAIRING_SQL,
        id="00000000-0000-0000-0000-0000000003a2",
        tenant=_TENANT,
        name="addon-new",
        client="pairing-client-new",
        service_sub=None,
        at=created,
    )
    for index, (table, client) in enumerate(
        (("agent_principal", "agent-client"), ("runner_principal", "runner-client"))
    ):
        _run(
            sync_url,
            _PRINCIPAL_SQL.format(table=table),
            id=f"00000000-0000-0000-0000-0000000003b{index}",
            tenant=_TENANT,
            name=table,
            client=client,
            at=created,
        )
    for index, sub in enumerate(_PAIRED_EDITS):
        _run(
            sync_url,
            AUDIT_SQL,
            id=f"00000000-0000-0000-0000-0000000004{index:02d}",
            at=f"2026-11-0{index + 1} 10:00:00",
            sub=sub,
            status=200,
            payload=audit_payload("agent.edit", "paired", sub),
            tenant=_TENANT,
        )

    command.upgrade(cfg, _REVISION)

    rows = _run(
        sync_url,
        "SELECT updated_by_sub FROM agent_definition WHERE id = :id",
        id=_PAIRED_DEFINITION,
    )
    assert rows == [("editor-person",)]
    assert _run(sync_url, "SELECT sub FROM machine_login") == []
