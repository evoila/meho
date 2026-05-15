# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""End-to-end tests for the audit-query handler (G8.1-T1).

The tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`, which migrates a
fresh per-test DB to head before each test. Rows are seeded directly via
``get_sessionmaker()`` — the chassis :class:`~meho_backplane.audit.AuditMiddleware`
is not exercised here because T1's contract is the **read** substrate; the
write paths are covered by ``tests/test_audit_middleware.py``.

Coverage matrix (G8.1-T1 acceptance criteria):

* AC3 — Tenant-scoping baked in: seed audit rows on two tenants; query one;
  cross-tenant rows never returned.
* AC4 — Every filter combination narrows results correctly.
* AC5 — Cursor pagination forward-only: 250 rows / ``limit=100`` produces 3
  pages, last page ``next_cursor=None``.
* AC6 — ``op_id="vsphere.vm.*"`` glob matches MCP payload rows correctly.
* AC8 — ``parent_audit_id`` filter raises :class:`UnsupportedFilterError`.
* :class:`InvalidCursorError` propagates when a tampered cursor is passed.
* ``target_name`` denormalization via LEFT JOIN works (and is None when the
  audit row has no ``target_id``).
* ``result_status`` derivation mirrors the broadcast classifier trichotomy.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.audit_query import (
    AuditQueryFilters,
    InvalidCursorError,
    UnsupportedFilterError,
    decode_cursor,
    query_audit,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Target
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Keycloak + Vault env vars :class:`Settings` requires."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """One :class:`AsyncSession` per test, scoped to a single ``async with``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_audit_row(
    s: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    occurred_at: datetime,
    operator_sub: str = "operator-1",
    method: str = "GET",
    path: str = "/api/v1/healthz",
    status_code: int = 200,
    target_id: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
) -> uuid.UUID:
    """Insert one :class:`AuditLog` row and return its ``id``."""
    row_id = uuid.uuid4()
    s.add(
        AuditLog(
            id=row_id,
            occurred_at=occurred_at,
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=Decimal("1.0"),
            payload=payload or {},
            target_id=target_id,
        ),
    )
    return row_id


async def _seed_target(s: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert one :class:`Target` row and return its ``id``."""
    target_id = uuid.uuid4()
    s.add(
        Target(
            id=target_id,
            tenant_id=tenant_id,
            name=name,
            aliases=[],
            product="vsphere",
            host=f"{name}.example.com",
            auth_model="shared_service_account",
            extras={},
        ),
    )
    return target_id


# ---------------------------------------------------------------------------
# Tenant scoping (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoping_isolates_rows(session: AsyncSession) -> None:
    """Seed two tenants, query one — only its rows are returned."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    base = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed_audit_row(
            session,
            tenant_id=tenant_a,
            occurred_at=base + timedelta(seconds=i),
            operator_sub="alice",
        )
        await _seed_audit_row(
            session,
            tenant_id=tenant_b,
            occurred_at=base + timedelta(seconds=i),
            operator_sub="bob",
        )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_a, session=session)

    assert len(result.rows) == 5
    assert all(entry.tenant_id == tenant_a for entry in result.rows)
    assert all(entry.principal_sub == "alice" for entry in result.rows)


@pytest.mark.asyncio
async def test_tenant_scoping_ignores_tenant_id_on_filter_object(
    session: AsyncSession,
) -> None:
    """The handler's ``tenant_id`` argument is the only authority.

    Constructing :class:`AuditQueryFilters` cannot smuggle a tenant id
    through because the model does not carry one.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_audit_row(session, tenant_id=tenant_b, occurred_at=datetime.now(UTC))
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_a, session=session)
    assert result.rows == []


@pytest.mark.asyncio
async def test_tenant_scoping_target_name_does_not_leak_cross_tenant(
    session: AsyncSession,
) -> None:
    """Cross-tenant ``target_id`` resolves to ``target_name=None``, not the
    other tenant's name.

    ``audit_log.target_id`` is a soft column (no FK in v0.2 per the chassis
    discipline), so a write-path bug could in principle persist a target_id
    that belongs to a different tenant. The read substrate must scope the
    LEFT JOIN by ``Target.tenant_id`` so the denormalized ``target_name``
    never surfaces another tenant's data — even if the rest of the audit row
    is correctly tenant-scoped, leaking the target name alone is a
    tenant-isolation violation.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    cross_target_id = await _seed_target(session, tenant_id=tenant_b, name="tenant-b-secret")
    own_target_id = await _seed_target(session, tenant_id=tenant_a, name="tenant-a-public")
    await session.commit()

    await _seed_audit_row(
        session,
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        target_id=cross_target_id,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        target_id=own_target_id,
    )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_a, session=session)
    by_target = {entry.target_id: entry.target_name for entry in result.rows}

    # Cross-tenant target_id surfaces with target_name=None — the JOIN's
    # tenant scope filtered out tenant B's target row.
    assert by_target[cross_target_id] is None
    # Same-tenant target_id resolves normally.
    assert by_target[own_target_id] == "tenant-a-public"


# ---------------------------------------------------------------------------
# Cursor pagination (AC5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_pagination_walks_250_rows_in_3_pages(
    session: AsyncSession,
) -> None:
    """250 rows / ``limit=100`` → 3 pages, terminal page ``next_cursor=None``."""
    tenant_id = uuid.uuid4()
    base = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    seeded: list[uuid.UUID] = []
    for i in range(250):
        seeded.append(
            await _seed_audit_row(
                session,
                tenant_id=tenant_id,
                occurred_at=base + timedelta(seconds=i),
            ),
        )
    await session.commit()

    all_ids: list[uuid.UUID] = []
    cursor: str | None = None
    pages = 0
    while True:
        pages += 1
        result = await query_audit(
            AuditQueryFilters(limit=100, cursor=cursor),
            tenant_id=tenant_id,
            session=session,
        )
        all_ids.extend(entry.id for entry in result.rows)
        if result.next_cursor is None:
            break
        cursor = result.next_cursor

    assert pages == 3
    assert len(all_ids) == 250
    # Every seeded id appears exactly once
    assert set(all_ids) == set(seeded)


@pytest.mark.asyncio
async def test_cursor_pagination_terminal_page_when_under_limit(
    session: AsyncSession,
) -> None:
    """Fewer rows than ``limit`` → ``next_cursor`` is None immediately."""
    tenant_id = uuid.uuid4()
    for i in range(5):
        await _seed_audit_row(
            session,
            tenant_id=tenant_id,
            occurred_at=datetime(2026, 5, 14, 0, 0, i, tzinfo=UTC),
        )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(limit=100),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 5
    assert result.next_cursor is None


@pytest.mark.asyncio
async def test_cursor_invalid_token_raises(session: AsyncSession) -> None:
    """A tampered cursor raises :class:`InvalidCursorError` from the handler."""
    tenant_id = uuid.uuid4()
    with pytest.raises(InvalidCursorError):
        await query_audit(
            AuditQueryFilters(cursor="not%%base64$$"),
            tenant_id=tenant_id,
            session=session,
        )


@pytest.mark.asyncio
async def test_cursor_next_cursor_decodes_to_last_row(session: AsyncSession) -> None:
    """``next_cursor`` round-trips to the page's last ``(ts, id)`` pair."""
    tenant_id = uuid.uuid4()
    base = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    for i in range(5):
        await _seed_audit_row(
            session,
            tenant_id=tenant_id,
            occurred_at=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(limit=3),
        tenant_id=tenant_id,
        session=session,
    )

    assert result.next_cursor is not None
    last = result.rows[-1]
    pos = decode_cursor(result.next_cursor)
    # SQLite strips tzinfo on read-back; compare the naive form on both sides.
    assert pos.ts.replace(tzinfo=None) == last.ts.replace(tzinfo=None)
    assert pos.id == last.id


# ---------------------------------------------------------------------------
# op_id glob filter (AC6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_id_glob_matches_payload_prefix(session: AsyncSession) -> None:
    """``op_id="vsphere.vm.*"`` matches MCP rows whose payload op_id has the prefix."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "vsphere.vm.list", "op_class": "read"},
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "vsphere.vm.get", "op_class": "read"},
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 3, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "vsphere.host.list", "op_class": "read"},
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(op_id="vsphere.vm.*"),
        tenant_id=tenant_id,
        session=session,
    )

    op_ids = sorted(entry.op_id for entry in result.rows)
    assert op_ids == ["vsphere.vm.get", "vsphere.vm.list"]


@pytest.mark.asyncio
async def test_op_id_glob_matches_http_derived_op_id(session: AsyncSession) -> None:
    """``op_id="http.get:*"`` matches HTTP rows whose op_id is derived from method+path."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        method="GET",
        path="/api/v1/connectors",
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        method="POST",
        path="/api/v1/connectors",
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(op_id="http.get:*"),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].method == "GET"


@pytest.mark.asyncio
async def test_op_id_glob_escapes_sql_wildcards(session: AsyncSession) -> None:
    """A literal ``%`` in ``op_id`` is escaped, not interpreted as SQL wildcard.

    Without escaping the LIKE metacharacters in operator-controllable input,
    a filter like ``op_id="foo%bar"`` would match every op_id starting with
    ``foo`` and ending in ``bar`` (the embedded ``%`` would act as the SQL
    wildcard) rather than the exact literal substring. ``_`` has the same
    risk — it matches any single character in LIKE.
    """
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "foo%bar"},
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "fooXXXbar"},
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(op_id="foo%bar"),
        tenant_id=tenant_id,
        session=session,
    )

    # Only the literal-percent row matches; the wildcard-expansion candidate
    # is excluded because ``%`` in the filter was escaped to a literal.
    assert {entry.op_id for entry in result.rows} == {"foo%bar"}


@pytest.mark.asyncio
async def test_op_class_filter_against_payload(session: AsyncSession) -> None:
    """``op_class="write"`` matches MCP rows whose payload carries the value."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        path="/mcp",
        payload={"op_class": "write"},
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        path="/mcp",
        payload={"op_class": "read"},
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(op_class="write"),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].op_class == "write"


# ---------------------------------------------------------------------------
# Unsupported filters (AC8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_audit_id_filter_raises_unsupported(
    session: AsyncSession,
) -> None:
    """``parent_audit_id`` filter raises until the column lands (G0.6-T7 #398)."""
    with pytest.raises(UnsupportedFilterError):
        await query_audit(
            AuditQueryFilters(parent_audit_id=uuid.uuid4()),
            tenant_id=uuid.uuid4(),
            session=session,
        )


@pytest.mark.asyncio
async def test_agent_session_id_filter_raises_unsupported(
    session: AsyncSession,
) -> None:
    """``agent_session_id`` filter raises — no column on any current roadmap."""
    with pytest.raises(UnsupportedFilterError):
        await query_audit(
            AuditQueryFilters(agent_session_id=uuid.uuid4()),
            tenant_id=uuid.uuid4(),
            session=session,
        )


# ---------------------------------------------------------------------------
# Column-mapped filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_id_exact_lookup(session: AsyncSession) -> None:
    """``audit_id`` returns the single matching row."""
    tenant_id = uuid.uuid4()
    target_audit_id = await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(audit_id=target_audit_id),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].id == target_audit_id


@pytest.mark.asyncio
async def test_principal_partial_match(session: AsyncSession) -> None:
    """``principal="ali"`` matches ``alice`` but not ``bob``."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        operator_sub="alice",
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        operator_sub="bob",
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(principal="ali"),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].principal_sub == "alice"


@pytest.mark.asyncio
async def test_principal_escapes_sql_wildcards(session: AsyncSession) -> None:
    """Literal ``_`` / ``%`` in ``principal`` matches the character, not a wildcard.

    Mirror of ``test_op_id_glob_escapes_sql_wildcards`` for the ``principal``
    filter. Without escaping the LIKE metacharacters, a filter value like
    ``"user_test"`` would also match ``"userXtest"`` (``_`` acting as the
    SQL single-character wildcard) — a real concern for deployments that
    use username-style JWT subs rather than UUIDs.
    """
    tenant_id = uuid.uuid4()
    for operator_sub in ("user_test", "userXtest", "alice%bob", "aliceXbob"):
        await _seed_audit_row(
            session,
            tenant_id=tenant_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
        )
    await session.commit()

    # Literal underscore — only the exact ``user_test`` row matches.
    result = await query_audit(
        AuditQueryFilters(principal="user_test"),
        tenant_id=tenant_id,
        session=session,
    )
    assert {entry.principal_sub for entry in result.rows} == {"user_test"}

    # Literal percent — only the exact ``alice%bob`` row matches.
    result = await query_audit(
        AuditQueryFilters(principal="alice%bob"),
        tenant_id=tenant_id,
        session=session,
    )
    assert {entry.principal_sub for entry in result.rows} == {"alice%bob"}


@pytest.mark.asyncio
async def test_since_until_range(session: AsyncSession) -> None:
    """``since`` and ``until`` bracket the ``occurred_at`` range."""
    tenant_id = uuid.uuid4()
    base = datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    for i in range(10):
        await _seed_audit_row(
            session,
            tenant_id=tenant_id,
            occurred_at=base + timedelta(seconds=i),
        )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(since=base + timedelta(seconds=3), until=base + timedelta(seconds=7)),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 5  # seconds 3, 4, 5, 6, 7


@pytest.mark.asyncio
async def test_target_filter_resolves_by_name(session: AsyncSession) -> None:
    """``target="rdc-vcenter"`` resolves through the targets table by name."""
    tenant_id = uuid.uuid4()
    target_id = await _seed_target(session, tenant_id=tenant_id, name="rdc-vcenter")
    other_target_id = await _seed_target(session, tenant_id=tenant_id, name="rdc-vault")
    await session.commit()

    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        target_id=target_id,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        target_id=other_target_id,
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(target="rdc-vcenter"),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].target_id == target_id
    assert result.rows[0].target_name == "rdc-vcenter"


@pytest.mark.asyncio
async def test_target_name_denormalization_left_join(session: AsyncSession) -> None:
    """The LEFT JOIN populates ``target_name`` when present, None otherwise."""
    tenant_id = uuid.uuid4()
    target_id = await _seed_target(session, tenant_id=tenant_id, name="rdc-vcenter")
    await session.commit()

    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        target_id=target_id,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        target_id=None,
    )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_id, session=session)

    by_target = {entry.target_id: entry.target_name for entry in result.rows}
    assert by_target[target_id] == "rdc-vcenter"
    assert by_target[None] is None


# ---------------------------------------------------------------------------
# result_status derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_status_filter_ok(session: AsyncSession) -> None:
    """``result_status="ok"`` matches 2xx/3xx, excludes 4xx/5xx."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        status_code=200,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        status_code=403,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 3, tzinfo=UTC),
        status_code=500,
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(result_status="ok"),
        tenant_id=tenant_id,
        session=session,
    )

    assert len(result.rows) == 1
    assert result.rows[0].status_code == 200
    assert result.rows[0].result_status == "ok"


@pytest.mark.asyncio
async def test_result_status_filter_denied(session: AsyncSession) -> None:
    """``result_status="denied"`` matches 401 + 403 exactly."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        status_code=401,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        status_code=403,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 3, tzinfo=UTC),
        status_code=500,
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(result_status="denied"),
        tenant_id=tenant_id,
        session=session,
    )

    assert sorted(entry.status_code for entry in result.rows) == [401, 403]


@pytest.mark.asyncio
async def test_result_status_filter_error(session: AsyncSession) -> None:
    """``result_status="error"`` matches 4xx/5xx except 401/403."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        status_code=400,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 2, tzinfo=UTC),
        status_code=403,
    )
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 3, tzinfo=UTC),
        status_code=500,
    )
    await session.commit()

    result = await query_audit(
        AuditQueryFilters(result_status="error"),
        tenant_id=tenant_id,
        session=session,
    )

    assert sorted(entry.status_code for entry in result.rows) == [400, 500]


# ---------------------------------------------------------------------------
# Computed fields populate correctly in AuditEntry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_computed_op_id_for_http_row(session: AsyncSession) -> None:
    """HTTP rows without payload op_id derive ``http.<method>:<path>``."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        method="POST",
        path="/api/v1/connectors/vsphere/vm.list",
    )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_id, session=session)
    assert result.rows[0].op_id == "http.post:/api/v1/connectors/vsphere/vm.list"


@pytest.mark.asyncio
async def test_computed_op_class_classifies_audit_query(session: AsyncSession) -> None:
    """A row whose computed op_id starts with ``audit.`` classifies as ``audit_query``."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
        path="/mcp",
        payload={"op_id": "audit.query"},
    )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_id, session=session)
    assert result.rows[0].op_class == "audit_query"


@pytest.mark.asyncio
async def test_placeholders_always_none(session: AsyncSession) -> None:
    """The four v0.2 placeholder fields are always None on the returned entry."""
    tenant_id = uuid.uuid4()
    await _seed_audit_row(
        session,
        tenant_id=tenant_id,
        occurred_at=datetime(2026, 5, 14, 0, 0, 1, tzinfo=UTC),
    )
    await session.commit()

    result = await query_audit(AuditQueryFilters(), tenant_id=tenant_id, session=session)
    entry = result.rows[0]
    assert entry.principal_name is None
    assert entry.parent_audit_id is None
    assert entry.agent_session_id is None
    assert entry.broadcast_event_id is None
