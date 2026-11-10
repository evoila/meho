# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A login setup is audited before calls use it, and compared with the saved row.

* The audit row of a new or replaced login setup commits before the
  scope's calls use the class. When the audit write fails, the calls keep
  the old login setup and the saved row keeps the old login type, so a
  retry saves, audits and applies again.
* An ingest compares the new login type with the scope's saved row, not
  with the memory of the replica that takes the ingest. Another replica
  may have saved another login type, or this replica may hold no class.
* When the saved row already holds the login type but this replica's
  table does not, the ingest puts the class in place with an audit row.

The tests run against ``sqlite+aiosqlite`` through the autouse fixtures in
:mod:`tests.conftest`. Names and secrets are made up.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from meho_backplane.connectors.registry import deregister_tenant_connector, get_tenant_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import IngestedAuthProfile
from meho_backplane.operations.ingest import service as service_module
from meho_backplane.operations.ingest.ingest_auth_store import (
    restore_ingested_profiled_connectors,
)
from meho_backplane.operations.ingest.service import ReviewService
from tests.test_operations_login_setup_tenants import (  # noqa: F401 (autouse fixtures)
    _CONNECTOR_ID,
    _PRODUCT,
    _TRIPLE,
    _clean_registry,
    _ingest,
    _operator,
    _required_settings_env,
    _resolve,
    _scheme,
    _stamp_audit_payloads,
)


def _break_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        raise OperationalError("INSERT INTO audit_log", {}, Exception("audit write failed"))

    monkeypatch.setattr(service_module, "audit_profile_stamp", _fail)


async def _saved_scheme(tenant_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        stmt = select(IngestedAuthProfile.auth_scheme).where(
            IngestedAuthProfile.tenant_id == tenant_id
        )
        return (await session.execute(stmt)).scalar_one()


async def _save_on_another_replica(auth_scheme: str, secret_fields: list[str]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(IngestedAuthProfile).values(auth_scheme=auth_scheme, secret_fields=secret_fields)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# The audit row commits first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_audit_on_ingest_keeps_the_old_login_type_and_a_retry_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    with monkeypatch.context() as broken:
        _break_audit(broken)
        with pytest.raises(OperationalError):
            await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="static_header")

    assert _scheme(await _resolve(tenant_a)) == "basic"
    assert await _saved_scheme(tenant_a) == "basic"
    assert len(await _stamp_audit_payloads()) == 1

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="static_header")

    assert result.ingestion.login_setup == "replaced"
    assert _scheme(await _resolve(tenant_a)) == "static_header"
    last = (await _stamp_audit_payloads())[-1]
    assert last["previous_auth_scheme"] == "basic"
    assert last["auth_scheme"] == "static_header"


@pytest.mark.asyncio
async def test_failed_audit_on_first_ingest_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()

    with monkeypatch.context() as broken:
        _break_audit(broken)
        with pytest.raises(OperationalError):
            await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert get_tenant_connector(tenant_a, *_TRIPLE) is None

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert result.ingestion.login_setup == "registered"
    assert _scheme(await _resolve(tenant_a)) == "basic"
    assert len(await _stamp_audit_payloads()) == 1


@pytest.mark.asyncio
async def test_failed_audit_on_restore_and_before_a_call_keeps_the_old_login_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    await _save_on_another_replica("static_header", ["token"])

    with monkeypatch.context() as broken:
        _break_audit(broken)
        with pytest.raises(OperationalError):
            await restore_ingested_profiled_connectors(product=_PRODUCT, tenant_id=tenant_a)
        # A call runs the same restore, logs the database error and goes on
        # with the login type that was audited.
        assert _scheme(await _resolve(tenant_a)) == "basic"

    assert len(await _stamp_audit_payloads()) == 1

    assert await restore_ingested_profiled_connectors(product=_PRODUCT, tenant_id=tenant_a) == 1
    assert _scheme(await _resolve(tenant_a)) == "static_header"
    assert (await _stamp_audit_payloads())[-1]["previous_auth_scheme"] == "basic"


# ---------------------------------------------------------------------------
# A second ingest on a replica with an old or no class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_on_a_stale_replica_compares_with_the_saved_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    # Another replica saved another login type; this replica still holds basic.
    await _save_on_another_replica("static_header", ["token"])

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert result.ingestion.login_setup == "replaced"
    assert result.ingestion.connector_registered is True
    assert await _saved_scheme(tenant_a) == "basic"
    assert _scheme(await _resolve(tenant_a)) == "basic"
    last = (await _stamp_audit_payloads())[-1]
    assert last["replaced"] is True
    assert last["previous_auth_scheme"] == "static_header"
    assert last["previous_secret_fields"] == ["token"]
    assert last["auth_scheme"] == "basic"


@pytest.mark.asyncio
async def test_ingest_on_a_replica_without_the_class_keeps_the_old_login_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    await _save_on_another_replica("static_header", ["token"])
    deregister_tenant_connector(
        tenant_id=tenant_a, product=_TRIPLE[0], version=_TRIPLE[1], impl_id=_TRIPLE[2]
    )

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert result.ingestion.login_setup == "replaced"
    assert (await _stamp_audit_payloads())[-1]["previous_auth_scheme"] == "static_header"
    assert _scheme(await _resolve(tenant_a)) == "basic"


@pytest.mark.asyncio
async def test_same_login_type_on_a_replica_without_the_class_registers_with_an_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    deregister_tenant_connector(
        tenant_id=tenant_a, product=_TRIPLE[0], version=_TRIPLE[1], impl_id=_TRIPLE[2]
    )

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert result.ingestion.login_setup == "registered"
    payloads = await _stamp_audit_payloads()
    assert len(payloads) == 2
    assert payloads[-1]["tenant_scope"] == str(tenant_a)
    assert payloads[-1]["replaced"] is False
    # The class is back in this replica's table.
    assert _scheme(await _resolve(tenant_a)) == "basic"


@pytest.mark.asyncio
async def test_same_saved_login_type_on_a_replica_with_another_class_replaces_with_an_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    # Another replica saved static_header; this replica still holds basic.
    await _save_on_another_replica("static_header", ["token"])

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="static_header")

    assert result.ingestion.login_setup == "replaced"
    assert _scheme(await _resolve(tenant_a)) == "static_header"
    last = (await _stamp_audit_payloads())[-1]
    assert last["replaced"] is True
    assert last["previous_auth_scheme"] == "basic"
    assert last["auth_scheme"] == "static_header"


@pytest.mark.asyncio
async def test_tenant_login_type_saved_while_a_built_in_one_served_is_audited_when_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_b = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=None, auth_scheme="basic")
    served = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="session_login")
    assert served.ingestion.login_setup == "served_by_builtin_login_setup"
    assert [p["tenant_scope"] for p in await _stamp_audit_payloads()] == [None]

    await ReviewService(_operator(None)).delete_connector(_CONNECTOR_ID, tenant_id=None)
    assert get_tenant_connector(tenant_b, *_TRIPLE) is None

    result = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="session_login")

    assert result.ingestion.login_setup == "registered"
    assert result.ingestion.connector_registered is True
    last = (await _stamp_audit_payloads())[-1]
    assert last["tenant_scope"] == str(tenant_b)
    assert last["auth_scheme"] == "session_login"
    assert _scheme(await _resolve(tenant_b)) == "session_login"
