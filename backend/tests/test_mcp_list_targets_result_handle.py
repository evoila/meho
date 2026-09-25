# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``list_targets`` applies the JSONFlux result-handle threshold (#3858).

Field test #3143 F10: on a tenant with 89 targets the default-params
``list_targets`` call returned all 89 rows inline, above the size at
which CLAUDE.md postulate 6 says a set-shaped result becomes a handle.
The handler now runs its page through the same
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`
(and default thresholds) ``call_operation`` uses:

* a page at or under the threshold keeps the inline
  ``{targets, next_cursor}`` shape;
* a page over it returns the reducer summary plus a ``handle`` whose
  rows ``result_query`` reads back, including for a ``platform_admin``
  listing another tenant (the spill is keyed to the caller);
* the listing is one SELECT over the five projected columns, so the
  per-row ``fingerprint`` JSON is never loaded.

The result-handle store is backed by an in-memory Valkey stand-in wired
into both the reducer (spill) and the ``result_query`` core (read back).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.mcp.tools.topology as topology_tools
import meho_backplane.operations.jsonflux_reducer as jsonflux_reducer_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000b0")

#: The field-test tenant size (#3143 F10).
_FIELD_TEST_TARGETS = 89


class _MemoryValkey:
    """Minimal async Valkey shape the :class:`ResultHandleStore` reads and writes."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture(autouse=True)
def result_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """One store wired into both the reducer's spill and ``result_query``'s read."""
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(jsonflux_reducer_module, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return store


async def _seed_targets(tenant_id: UUID, slug: str, count: int) -> list[str]:
    """Seed a tenant with *count* targets carrying a fingerprint + CA pin; return the names."""
    names = [f"lab-{i:03d}" for i in range(count)]
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=slug, name=slug))
        for i, name in enumerate(names):
            session.add(
                TargetORM(
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[f"{name}-alias"],
                    product=("vmware", "k8s", "vault")[i % 3],
                    host=f"{name}.lab.example",
                    port=443,
                    secret_ref=f"targets/{name}",
                    auth_model="shared_service_account",
                    fingerprint={"product": "vcenter", "version": "9.0", "reachable": True},
                    tls_ca_pin="-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
                )
            )
    return names


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run one ``tools/call``; return ``(structuredContent, text block)``."""
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    body = response.json()
    assert "error" not in body, body
    text = body["result"]["content"][0]["text"]
    return json.loads(text), text


@pytest.fixture
def handler_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every statement the ``list_targets`` handler's own sessions execute.

    Patched on the tool module (the usage site) rather than as an engine
    listener: the lifespan's background topology sweep reads ``targets``
    on the same engine and would race into an engine-wide capture.
    """
    statements: list[str] = []
    sessionmaker = get_sessionmaker()

    @asynccontextmanager
    async def _recording_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            execute = session.execute

            async def _record(statement: Any, *args: Any, **kwargs: Any) -> Any:
                statements.append(str(statement))
                return await execute(statement, *args, **kwargs)

            session.execute = _record  # type: ignore[method-assign]
            yield session

    monkeypatch.setattr(topology_tools, "get_sessionmaker", lambda: _recording_session)
    return statements


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_default_page_over_threshold_returns_handle_not_inline_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """89 targets at the default page size come back as a handle + summary."""
    client, _op = client_with_operator
    await _seed_targets(OPERATOR_TENANT_ID, "op-tenant", _FIELD_TEST_TARGETS)

    payload, text = _call(client, "list_targets", {})

    assert "targets" not in payload
    assert payload["row_count"] == _FIELD_TEST_TARGETS
    assert payload["next_cursor"] is None
    handle = payload["handle"]
    assert handle["total_rows"] == _FIELD_TEST_TARGETS
    assert 0 < len(handle["sample_rows"]) <= 5
    assert handle["fetch_more"]["drill_in"]["available"] is True
    assert handle["fetch_more"]["drill_in"]["mcp_tool"] == "result_query"
    assert handle["fetch_more"]["native_pagination"]["example_next_call"]["tool"] == "list_targets"
    # Inline, these 89 short fixture rows serialize to ~13 KB (and the
    # dispatcher emits the payload twice: text block + structuredContent).
    assert len(text) < 4096


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_reduced_page_rows_read_back_through_result_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Every row of the reduced page is readable via ``result_query`` (paging + query)."""
    client, _op = client_with_operator
    names = await _seed_targets(OPERATOR_TENANT_ID, "op-tenant", _FIELD_TEST_TARGETS)
    payload, _text = _call(client, "list_targets", {})
    handle_id = payload["handle"]["handle_id"]

    window, _ = _call(client, "result_query", {"handle_id": handle_id, "offset": 0, "limit": 100})
    assert [row["name"] for row in window["rows"]] == names

    queried, _ = _call(
        client,
        "result_query",
        {
            "handle_id": handle_id,
            "query": {
                "select": ["name"],
                "filter": [{"field": "product", "op": "=", "value": "vault"}],
            },
        },
    )
    assert [row["name"] for row in queried["rows"]] == names[2::3]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize(("seeded", "arguments"), [(10, {}), (_FIELD_TEST_TARGETS, {"limit": 10})])
async def test_page_under_threshold_stays_inline(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded: int,
    arguments: dict[str, Any],
) -> None:
    """A small tenant, or a small ``limit`` on a large one, keeps the inline shape."""
    client, _op = client_with_operator
    names = await _seed_targets(OPERATOR_TENANT_ID, "op-tenant", seeded)

    payload, _text = _call(client, "list_targets", arguments)

    assert "handle" not in payload
    assert [t["name"] for t in payload["targets"]] == names[:10]
    assert payload["next_cursor"] == (names[9] if seeded > 10 else None)


@pytest.mark.parametrize("client_with_operator", [(TenantRole.TENANT_ADMIN, True)], indirect=True)
async def test_cross_tenant_reduced_page_is_readable_by_the_caller(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A platform_admin's cross-tenant handle is spilled under the caller's own key."""
    client, _op = client_with_operator
    await _seed_targets(OPERATOR_TENANT_ID, "op-tenant", 0)
    names = await _seed_targets(_OTHER_TENANT_ID, "other-tenant", 60)

    payload, _text = _call(client, "list_targets", {"tenant_id": "other-tenant"})
    window, _ = _call(
        client,
        "result_query",
        {"handle_id": payload["handle"]["handle_id"], "offset": 0, "limit": 100},
    )

    assert [row["name"] for row in window["rows"]] == names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_listing_is_one_select_without_fingerprint_columns(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    handler_statements: list[str],
) -> None:
    """No per-row query and no ``fingerprint`` / CA-pin decode on the list path."""
    client, _op = client_with_operator
    await _seed_targets(OPERATOR_TENANT_ID, "op-tenant", _FIELD_TEST_TARGETS)

    _call(client, "list_targets", {})

    assert len(handler_statements) == 1
    assert "fingerprint" not in handler_statements[0]
    assert "tls_ca_pin" not in handler_statements[0]
