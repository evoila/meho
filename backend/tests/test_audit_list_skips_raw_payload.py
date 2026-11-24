# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit list and two console cards do not read ``raw_payload`` (#6818).

An audit row keeps the whole raw answer of a call in ``raw_payload``. One
row can hold many megabytes. Three list reads never show it, so they must
not load it:

* :func:`~meho_backplane.audit_query.query_audit` -- the audit list of REST,
  CLI, MCP and the console.
* ``_fetch_recent_ops`` -- the recent operations in the topology drawer.
* ``_load_recent_ops`` -- the recent operations card of a target page.

Each test seeds a row with a raw answer of about 5 MB and checks:

* no SQL statement that runs names the ``raw_payload`` column;
* the loaded rows have ``raw_payload`` not loaded, and a read of it fails;
* the answer is the same as the one built from fully loaded rows.

The policy replay still reads ``raw_payload``, also in a session where the
audit list loaded the same row first.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import AuditQueryFilters, query_audit, replay_policy
from meho_backplane.audit_query import query as query_module
from meho_backplane.audit_query.policy_replay import PolicyReplayStatus
from meho_backplane.audit_query.query import _build_audit_entry
from meho_backplane.db.engine import get_engine, get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.connectors.detail import _load_recent_ops, _project_audit_rows
from meho_backplane.ui.routes.topology.detail import _fetch_recent_ops

from ._audit_large_raw_seed import LARGE_RAW, TARGET_NAME, TENANT_NAME, full_rows, seed


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _SqlLog:
    """Collect every SQL statement the engine runs while active."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> _SqlLog:
        event.listen(get_engine().sync_engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(get_engine().sync_engine, "before_cursor_execute", self._record)

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.statements.append(statement)

    def assert_no_raw_payload(self) -> None:
        assert self.statements, "no SQL ran"
        assert not [s for s in self.statements if "raw_payload" in s]


def _assert_raw_payload_not_loaded(row: AuditLog) -> None:
    assert "raw_payload" in sa.inspect(row).unloaded
    with pytest.raises(InvalidRequestError):
        _ = row.raw_payload


def _columns(row: AuditLog) -> dict[str, Any]:
    """Every column of *row* except ``raw_payload``."""
    return {
        c.key: getattr(row, c.key)
        for c in sa.inspect(AuditLog).column_attrs
        if c.key != "raw_payload"
    }


async def test_query_audit_does_not_load_raw_payload(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed(uuid.uuid4())
    loaded: list[AuditLog] = []

    def _record(row: AuditLog, *args: Any, **kwargs: Any) -> Any:
        loaded.append(row)
        return _build_audit_entry(row, *args, **kwargs)

    monkeypatch.setattr(query_module, "_build_audit_entry", _record)
    with _SqlLog() as sql:
        result = await query_audit(AuditQueryFilters(), tenant_id=seeded.tenant_id, session=session)
    sql.assert_no_raw_payload()

    assert {row.id for row in loaded} == set(seeded.ids)
    for row in loaded:
        _assert_raw_payload_not_loaded(row)

    full = await full_rows(seeded.ids)
    expected = [
        _build_audit_entry(full[entry.id], TARGET_NAME, tenant_name=TENANT_NAME)
        for entry in result.rows
    ]
    assert [e.model_dump(mode="json") for e in result.rows] == [
        e.model_dump(mode="json") for e in expected
    ]
    assert [entry.id for entry in result.rows] == list(reversed(seeded.ids))


async def test_policy_replay_reads_raw_payload_after_the_list_in_one_session(
    session: AsyncSession,
) -> None:
    """The replay loads ``raw_payload`` of a row the list already loaded."""
    seeded = await seed(uuid.uuid4())
    await query_audit(AuditQueryFilters(), tenant_id=seeded.tenant_id, session=session)

    verdict = await replay_policy(seeded.ids[2], tenant_id=seeded.tenant_id, session=session)

    # The seeded rows name no redaction policy, so the replay stops there.
    # What counts: it read ``raw_payload`` without an error.
    assert verdict.status is PolicyReplayStatus.REPLAY_NOT_APPLICABLE
    row = await session.get(AuditLog, seeded.ids[2])
    assert row is not None
    assert row.raw_payload == LARGE_RAW


async def test_topology_recent_ops_does_not_load_raw_payload(session: AsyncSession) -> None:
    seeded = await seed(uuid.uuid4())

    with _SqlLog() as sql:
        rows = await _fetch_recent_ops(
            session, tenant_id=seeded.tenant_id, target_id=seeded.target_id
        )
    sql.assert_no_raw_payload()

    assert [row.id for row in rows] == list(reversed(seeded.ids))
    full = await full_rows(seeded.ids)
    for row in rows:
        _assert_raw_payload_not_loaded(row)
        assert _columns(row) == _columns(full[row.id])


async def test_connector_recent_ops_does_not_load_raw_payload(session: AsyncSession) -> None:
    seeded = await seed(uuid.uuid4())

    with _SqlLog() as sql:
        rows = await _load_recent_ops(
            session, tenant_id=seeded.tenant_id, target_id=seeded.target_id
        )
        projected = _project_audit_rows(rows)
    sql.assert_no_raw_payload()

    assert [row.id for row in rows] == list(reversed(seeded.ids))
    for row in rows:
        _assert_raw_payload_not_loaded(row)
    full = await full_rows(seeded.ids)
    assert projected == _project_audit_rows(full[row.id] for row in rows)
