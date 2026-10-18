# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast is agent-intent only: the #3758 cut-over.

What this file pins:

* **No automatic publisher.** Inside the package, only the announcement
  publisher writes to the Broadcast stream. No module builds a
  ``BroadcastEvent`` or imports a publish helper, and a dispatch audit
  write touches no Valkey client.
* **Readers use the archive.** The SSE feed, the UI history pane and
  the MCP tenant feed read stored announcements; the approval waiter
  reads the stored approval row.
* **Dashboard recent operations.** ``GET /ui/dashboard/recent-operations``
  reads the tenant's ``operation_execution`` audit rows through the
  audit reader: tenant-scoped, at most 50, newest first, with an
  "Updated" status line and no live label.
* **Console polling.** The bell and the recent-operations cards refresh
  from external scripts with plain timers, only while the page is
  visible, with no inline htmx filter expression.
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meho_backplane
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.dashboard import build_dashboard_router
from meho_backplane.ui.templating import reset_templating_for_testing

_PACKAGE = Path(meho_backplane.__file__).parent
_STATIC = _PACKAGE / "ui" / "static" / "src" / "app"
_TEMPLATES = _PACKAGE / "ui" / "templates"
_PUBLISHER = _PACKAGE / "broadcast" / "publisher.py"

_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000003758")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000003758")


# ---------------------------------------------------------------------------
# No automatic publisher
# ---------------------------------------------------------------------------


def _python_files() -> Iterator[Path]:
    yield from sorted(_PACKAGE.rglob("*.py"))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_no_module_builds_a_broadcast_event() -> None:
    """Nothing constructs the old automatic ``BroadcastEvent`` any more."""
    offenders = [
        f"{path.relative_to(_PACKAGE)}:{node.lineno}"
        for path in _python_files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and _call_name(node) == "BroadcastEvent"
    ]
    assert offenders == []


def test_only_the_announcement_publisher_writes_the_stream() -> None:
    """``xadd`` on the broadcast stream happens in the publisher only."""
    offenders = [
        f"{path.relative_to(_PACKAGE)}:{node.lineno}"
        for path in _python_files()
        if path != _PUBLISHER
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and _call_name(node) == "xadd"
    ]
    assert offenders == []


def test_no_module_imports_a_removed_publisher() -> None:
    removed = {
        "publish_event",
        "publish_approval_event",
        "publish_broadcast",
        "audit_and_broadcast_safe",
        "publish_check_transition_event",
        "publish_scheduler_liveness_event",
    }
    offenders = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "meho_backplane"
            ):
                hit = removed & {alias.name for alias in node.names}
                if hit:
                    offenders.append(f"{path.relative_to(_PACKAGE)}: {sorted(hit)}")
    assert offenders == []
    assert not (_PACKAGE / "checks" / "broadcast.py").exists()


async def test_dispatch_audit_write_touches_no_valkey(
    _ui_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``audit_safe`` writes the row and never asks for a broadcast client."""
    from meho_backplane.auth.operator import Operator, TenantRole
    from meho_backplane.broadcast import client as client_module
    from meho_backplane.operations import _audit

    def _no_valkey() -> object:
        raise AssertionError("an automatic write reached for Valkey")

    monkeypatch.setattr(client_module, "get_broadcast_client", _no_valkey)
    monkeypatch.setattr(client_module, "get_broadcast_blocking_client", _no_valkey)

    class _Descriptor:
        op_id = "vsphere.vm.list"
        source_kind = "typed"
        product = "vmware"
        version = "9.0"
        impl_id = ""
        safety_level = "read"
        requires_approval = False

    operator = Operator(
        sub="op-1", raw_jwt="x", tenant_id=_TENANT_A, tenant_role=TenantRole.OPERATOR
    )
    audit_id = uuid.uuid4()
    receipt = await _audit.audit_safe(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=operator,
        descriptor=_Descriptor(),  # type: ignore[arg-type]
        target=None,
        params_hash="h",
        result_status="ok",
        duration_ms=1.0,
        require_audit=True,
    )
    assert receipt == audit_id
    async with get_sessionmaker()() as session:
        assert await session.get(AuditLog, audit_id) is not None


# ---------------------------------------------------------------------------
# Dashboard recent operations (Audit reader)
# ---------------------------------------------------------------------------


@pytest.fixture
def _ui_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_templating_for_testing()
    yield
    reset_templating_for_testing()
    get_settings.cache_clear()


def _client(tenant_id: uuid.UUID) -> TestClient:
    app = FastAPI()
    app.include_router(build_dashboard_router())
    app.dependency_overrides[require_ui_session] = lambda: UISessionContext(
        session_id=uuid.uuid4(), operator_sub="op-ui", tenant_id=tenant_id
    )
    return TestClient(app)


async def _seed_audit(
    *,
    tenant_id: uuid.UUID,
    path: str,
    event_class: AuditEventClass = AuditEventClass.OPERATION_EXECUTION,
    occurred_at: datetime | None = None,
) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AuditLog(
                id=uuid.uuid4(),
                occurred_at=occurred_at or datetime.now(UTC),
                operator_sub="op-1",
                method="DISPATCH",
                path=path,
                status_code=200,
                request_id=None,
                duration_ms=Decimal("1.00"),
                payload={"op_id": path},
                tenant_id=tenant_id,
                event_class=event_class.value,
            )
        )
        await session.commit()


async def test_dashboard_fragment_lists_tenant_operations_newest_first(_ui_env: None) -> None:
    base = datetime.now(UTC)
    await _seed_audit(tenant_id=_TENANT_A, path="op.first", occurred_at=base)
    await _seed_audit(
        tenant_id=_TENANT_A, path="op.second", occurred_at=base + timedelta(seconds=1)
    )
    await _seed_audit(tenant_id=_TENANT_B, path="op.other.tenant")
    await _seed_audit(
        tenant_id=_TENANT_A, path="approval.decision", event_class=AuditEventClass.APPROVAL_DECISION
    )
    await _seed_audit(
        tenant_id=_TENANT_A, path="/api/v1/wrapper", event_class=AuditEventClass.LEGACY_REQUEST
    )

    response = _client(_TENANT_A).get("/ui/dashboard/recent-operations")

    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    assert 'id="dashboard-recent-ops"' in body
    assert 'data-refresh-url="/ui/dashboard/recent-operations"' in body
    assert "Updated" in body
    assert "live" not in body.lower().replace("deliver", "")
    assert body.index("op.second") < body.index("op.first")
    assert "op.other.tenant" not in body
    assert "approval.decision" not in body
    assert "/api/v1/wrapper" not in body


async def test_dashboard_fragment_is_bounded_to_fifty(_ui_env: None) -> None:
    base = datetime.now(UTC)
    for index in range(55):
        await _seed_audit(
            tenant_id=_TENANT_A,
            path=f"op.n{index:02d}",
            occurred_at=base + timedelta(seconds=index),
        )

    body = _client(_TENANT_A).get("/ui/dashboard/recent-operations").text

    assert body.count("<li ") == 50
    assert "op.n54" in body
    assert "op.n04" not in body


async def test_dashboard_fragment_escapes_operation_text(_ui_env: None) -> None:
    await _seed_audit(tenant_id=_TENANT_A, path="<img src=x onerror=alert(1)>")

    body = _client(_TENANT_A).get("/ui/dashboard/recent-operations").text

    assert "<img src=x" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


async def test_dashboard_fragment_empty_state(_ui_env: None) -> None:
    body = _client(_TENANT_A).get("/ui/dashboard/recent-operations").text
    assert "No operations recorded yet." in body


# ---------------------------------------------------------------------------
# Console polling scripts and templates
# ---------------------------------------------------------------------------


def test_bell_uses_one_visible_page_timer_and_no_sse() -> None:
    script = (_STATIC / "approvals-bell.js").read_text(encoding="utf-8")
    assert script.count("setInterval(") == 1
    assert 'document.visibilityState === "visible"' in script
    assert "visibilitychange" in script
    assert "meho:approval-badge-poll" in script
    for sse_marker in ("EventSource", "sse-connect", "onSseMessage", "/ui/broadcast/stream"):
        assert sse_marker not in script


def test_recent_ops_refresh_script_polls_only_visible_pages() -> None:
    script = (_STATIC / "recent-ops-refresh.js").read_text(encoding="utf-8")
    assert "POLL_MS = 5000" in script
    assert script.count("setInterval(") == 1
    assert 'document.visibilityState !== "visible"' in script
    assert ".ajax(" in script
    assert "inFlight" in script
    assert not (_STATIC / "dashboard-feed.js").exists()
    assert not (_STATIC / "connectors-feed.js").exists()


def test_no_template_uses_an_inline_htmx_filter_or_feed_sse_for_operations() -> None:
    """No ``hx-trigger`` filter expression (needs eval under CSP); no ops SSE."""
    for path in (
        _TEMPLATES / "base.html",
        _TEMPLATES / "dashboard.html",
        _TEMPLATES / "_dashboard_recent_ops.html",
        _TEMPLATES / "connectors" / "_recent_ops.html",
        _TEMPLATES / "connectors" / "detail.html",
    ):
        text = path.read_text(encoding="utf-8")
        for trigger in re.findall(r'hx-trigger="([^"]*)"', text):
            assert "every" not in trigger, path
            assert "[" not in trigger, path
        assert "sse-connect" not in text, path
        assert "/ui/broadcast/stream" not in text, path
