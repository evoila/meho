# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings file page of the web console: upload, preview, apply.

* A person with ``tenant_admin`` turns ``run_approval_target_kinds`` on and
  ``announce_gate_enabled`` off with a settings file. The same file sent
  with a token to the REST route gets 403 and writes nothing
  (``test_settings_import_console_policies.py``).
* An agent, service or runner login, and a person without an admin role,
  cannot reach the page, the preview or the apply. Nothing is written.
* A ``tenant_admin`` cannot apply a file for another tenant (403, and a
  refusal row in the audit log); a ``platform_admin`` can.
* The preview shows every change, every mistake with its reason, the
  warnings next to the mistakes, and a note for each target the file adds
  or changes.
* The result names a full, a partial and a failed apply.
* The settings, the revision and the audit row of an apply commit
  together; when the audit write fails, nothing is saved and the answer is
  a 500 with an error text.
"""

from __future__ import annotations

import asyncio
import base64
import re
import uuid
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from starlette.middleware import Middleware

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db import engine as engine_module
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditEventClass,
    AuditLog,
    InstanceSettingsRevision,
    Tenant,
    TenantConvention,
)
from meho_backplane.instance_settings import service as settings_service
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes import settings as settings_routes
from tests import test_ui_agent_grants
from tests._oidc_jwt_helpers import mint_token
from tests._vault_fakes import install_fake_vault
from tests.test_ui_agent_grants import (
    _TENANT_A,
    _TENANT_B,
    _authenticated_client,
    _bff_env,  # noqa: F401  (autouse fixture)
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_session_sync,
)

_PAGE = "/ui/settings"
_PREVIEW = "/ui/settings/preview"
_APPLY = "/ui/settings/apply"
_KINDS = "spec.tenants[0].policies.run_approval_target_kinds"
_GATE = "spec.tenants[0].policies.announce_gate_enabled"


@pytest.fixture(autouse=True)
def _audited_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Add the audit middleware to the test app, closest to the routes as in production."""
    build = test_ui_agent_grants._build_app

    def _build() -> FastAPI:
        app = build()
        app.user_middleware.append(Middleware(AuditMiddleware))
        return app

    monkeypatch.setattr(test_ui_agent_grants, "_build_app", _build)


@pytest.fixture
def cutover_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``AUDIT_WRAPPER_ROWS_RETIRED=true``: the middleware writes no routine rows."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sqlite_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let SQLite open a real transaction, so a rollback undoes the savepoints too.

    The sqlite driver sends no ``BEGIN`` before a ``SAVEPOINT``, so releasing
    a savepoint commits it at once. This is the SQLAlchemy recipe for
    SQLite: the driver starts no transaction itself, and each SQLAlchemy
    transaction sends ``BEGIN``.
    """
    make = engine_module.create_engine_for_url

    def _make(url: str, *, pool_size: int, pool_timeout: float) -> Any:
        engine = make(url, pool_size=pool_size, pool_timeout=pool_timeout)

        @event.listens_for(engine.sync_engine, "connect")
        def _no_driver_transactions(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.isolation_level = None

        @event.listens_for(engine.sync_engine, "begin")
        def _begin(connection: Any) -> None:
            connection.exec_driver_sql("BEGIN")

        return engine

    monkeypatch.setattr(engine_module, "create_engine_for_url", _make)


def _seed_tenants() -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            for tenant_id, slug in ((_TENANT_A, "tenant-a"), (_TENANT_B, "tenant-b")):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        name=f"Tenant {slug}",
                        announce_gate_enabled=True,
                        run_approval_target_kinds_enabled=False,
                    )
                )

    asyncio.run(_do())


async def _load(tenant_id: uuid.UUID) -> Tenant:
    async with get_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    return tenant


async def _all(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


def _state(tenant_id: uuid.UUID = _TENANT_A) -> tuple[bool, bool, int, int]:
    """(gate on, target kinds on, conventions from a test file, revisions)."""

    async def _do() -> tuple[bool, bool, int, int]:
        tenant = await _load(tenant_id)
        return (
            tenant.announce_gate_enabled,
            tenant.run_approval_target_kinds_enabled,
            len([c for c in await _all(TenantConvention) if c.slug.startswith("rule-")]),
            len(await _all(InstanceSettingsRevision)),
        )

    return asyncio.run(_do())


_UNCHANGED = (True, False, 0, 0)


def _file(
    policies: dict[str, Any] | None, *, tenant_id: uuid.UUID = _TENANT_A, **extra: Any
) -> bytes:
    tenant: dict[str, Any] = {"id": str(tenant_id), "policies": policies, **extra}
    doc = {"apiVersion": "meho.evoila.com/v1", "kind": "Settings", "spec": {"tenants": [tenant]}}
    return yaml.safe_dump(doc, sort_keys=False).encode()


_KEEP: dict[str, Any] = {"announce_gate_enabled": True}
_LOOSEN = _file({"run_approval_target_kinds": True, "announce_gate_enabled": False})


def _client(
    *,
    sub: str = "op-alice",
    role: str = TenantRole.TENANT_ADMIN.value,
    principal_kind: str | None = None,
    platform_admin: bool | None = None,
    with_csrf: bool = True,
) -> tuple[Any, Any, str]:
    keypair, jwks = _make_keypair_and_jwks()
    extra: dict[str, Any] = {}
    if principal_kind == "runner":
        extra["runner_id"] = str(uuid.uuid4())
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=role,
        principal_kind=principal_kind,
        platform_admin=platform_admin,
        **extra,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=sub)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=with_csrf)


def _preview(client: Any, csrf: str, content: bytes) -> Any:
    return client.post(
        _PREVIEW,
        files={"file": ("meho-settings.yaml", content, "application/yaml")},
        headers=_csrf_headers(csrf),
    )


def _apply(client: Any, csrf: str, content: bytes) -> Any:
    encoded = base64.b64encode(content).decode("ascii")
    return client.post(
        _APPLY,
        files={"file_b64": (None, encoded)},
        headers=_csrf_headers(csrf),
    )


def _apply_field(html: str) -> str:
    match = re.search(r'name="file_b64" value="([^"]*)"', html)
    assert match is not None, html
    return match.group(1)


def test_a_person_in_the_console_widens_both_policies_with_a_file() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, _LOOSEN)
        assert preview.status_code == 200, preview.text
        assert _state() == _UNCHANGED
        html = preview.text
        assert f"`{_KINDS}` lets one run approval cover a whole target kind." in html
        assert f"`{_GATE}` turns the announce gate off." in html
        assert "An import with a token is refused" not in html
        assert 'data-kind="changed"' in html
        assert "run_approval_target_kinds" in html and "announce_gate_enabled" in html
        assert 'data-action="settings-apply"' in html

        encoded = _apply_field(html)
        applied = client.post(
            _APPLY, files={"file_b64": (None, encoded)}, headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    assert "MEHO applied the file." in applied.text
    assert _state() == (False, True, 0, 1)


def test_the_apply_writes_the_file_bytes_that_the_preview_showed() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, _LOOSEN)
    finally:
        mock.stop()
    assert base64.b64decode(_apply_field(preview.text)) == _LOOSEN


@pytest.mark.parametrize("kind", ["agent", "service", "runner"])
@pytest.mark.parametrize(("method", "path"), [("GET", _PAGE), ("POST", _PREVIEW), ("POST", _APPLY)])
def test_a_machine_login_cannot_reach_the_page(kind: str, method: str, path: str) -> None:
    _seed_tenants()
    client, mock, csrf = _client(sub=f"{kind}:deploy-bot", principal_kind=kind)
    try:
        if method == "GET":
            response = client.get(path, headers=_csrf_headers(csrf))
        elif path == _PREVIEW:
            response = _preview(client, csrf, _LOOSEN)
        else:
            response = _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "human_principal_required"}
    assert _state() == _UNCHANGED


@pytest.mark.parametrize("role", [TenantRole.OPERATOR.value, TenantRole.READ_ONLY.value])
@pytest.mark.parametrize("path", [_PAGE, _PREVIEW, _APPLY])
def test_a_person_without_an_admin_role_is_refused(role: str, path: str) -> None:
    _seed_tenants()
    client, mock, csrf = _client(role=role)
    try:
        if path == _PAGE:
            response = client.get(path, headers=_csrf_headers(csrf))
        elif path == _PREVIEW:
            response = _preview(client, csrf, _LOOSEN)
        else:
            response = _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}
    assert _state() == _UNCHANGED


def test_the_apply_needs_the_csrf_token() -> None:
    _seed_tenants()
    client, mock, _csrf = _client(with_csrf=False)
    try:
        encoded = base64.b64encode(_LOOSEN).decode("ascii")
        response = client.post(
            _APPLY, files={"file_b64": (None, encoded)}, headers={"HX-Request": "true"}
        )
    finally:
        mock.stop()
    assert response.status_code == 403, response.text
    assert _state() == _UNCHANGED


def test_a_tenant_admin_cannot_apply_a_file_for_another_tenant() -> None:
    _seed_tenants()
    other = _file({"announce_gate_enabled": False}, tenant_id=_TENANT_B)
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, other)
        applied = _apply(client, csrf, other)
    finally:
        mock.stop()

    for response in (preview, applied):
        assert response.status_code == 403, response.text
        assert response.headers["content-type"].startswith("text/html")
        assert "data-settings-refused" in response.text
        assert "a tenant_admin may import a file for their own tenant only" in response.text
        assert "data-settings-applied" not in response.text
    assert _state(_TENANT_B) == _UNCHANGED


def test_a_platform_admin_applies_a_file_for_another_tenant() -> None:
    _seed_tenants()
    other = _file({"announce_gate_enabled": False}, tenant_id=_TENANT_B)
    client, mock, csrf = _client(platform_admin=True)
    try:
        applied = _apply(client, csrf, other)
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    assert "MEHO applied the file." in applied.text
    assert _state(_TENANT_B)[0] is False
    assert _state(_TENANT_A)[0] is True


def test_the_preview_shows_every_mistake_and_offers_no_apply() -> None:
    _seed_tenants()
    bad = _file({"announce_gate_enabled": "sometimes", "no_such_policy": True})
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, bad)
        applied = _apply(client, csrf, bad)
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    mistakes = re.findall(r"<li data-settings-mistake>(.*?)</li>", preview.text, re.S)
    assert len(mistakes) >= 2, preview.text
    assert any("announce_gate_enabled" in m for m in mistakes)
    assert any("no_such_policy" in m for m in mistakes)
    assert 'data-action="settings-apply"' not in preview.text
    assert "data-settings-mistakes" in applied.text
    assert "data-settings-applied" not in applied.text
    assert _state() == _UNCHANGED


def test_the_preview_shows_every_change_without_a_cut() -> None:
    _seed_tenants()
    conventions = [
        {"slug": f"rule-{i:03d}", "title": f"Rule {i}", "body": "Nights only.", "kind": "reference"}
        for i in range(60)
    ]
    content = _file({"announce_gate_enabled": True}, conventions=conventions)
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, content)
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    assert preview.text.count('data-kind="added"') == 60
    for i in (0, 59):
        assert f"rule-{i:03d}" in preview.text
    assert _state() == _UNCHANGED


def test_the_apply_writes_every_change_of_a_long_file() -> None:
    _seed_tenants()
    conventions = [
        {"slug": f"rule-{i:03d}", "title": f"Rule {i}", "body": "Nights only.", "kind": "reference"}
        for i in range(60)
    ]
    content = _file({"announce_gate_enabled": True}, conventions=conventions)
    client, mock, csrf = _client()
    try:
        applied = _apply(client, csrf, content)
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    assert applied.text.count('data-kind="added"') == 60
    assert _state() == (True, False, 60, 1)


@pytest.mark.usefixtures("cutover_on")
def test_a_refusal_for_another_tenant_leaves_a_refusal_record() -> None:
    _seed_tenants()
    other = _file({"announce_gate_enabled": False}, tenant_id=_TENANT_B)
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, other)
        applied = _apply(client, csrf, other)
    finally:
        mock.stop()

    assert (preview.status_code, applied.status_code) == (403, 403)
    refusals = [
        row
        for row in asyncio.run(_all(AuditLog))
        if row.event_class == AuditEventClass.POLICY_DECISION and row.status_code == 403
    ]
    assert sorted(row.path for row in refusals) == [_APPLY, _PREVIEW]
    assert _state(_TENANT_B) == _UNCHANGED


def test_the_preview_shows_the_warnings_next_to_the_mistakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHART_VERSION", "0.1.0")
    _seed_tenants()
    content = _file(
        {"announce_gate_enabled": True},
        targets=[{"name": "app-01", "product": "no-such-product", "host": "h"}],
    )
    doc = yaml.safe_load(content)
    doc["metadata"] = {"exportedFrom": {"mehoVersion": "999.0.0"}}
    content = yaml.safe_dump(doc, sort_keys=False).encode()
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, content)
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    assert "data-settings-mistakes" in preview.text
    assert "spec.tenants[0].targets[0]" in preview.text
    assert "data-settings-warning" in preview.text
    assert "exported from a newer MEHO" in preview.text
    assert 'data-action="settings-apply"' not in preview.text


def test_the_preview_names_each_target_the_file_adds_or_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_vault(monkeypatch)
    _seed_tenants()
    stored = {"name": "app-01", "product": "linux", "host": "h1"}
    client, mock, csrf = _client()
    try:
        first = _apply(client, csrf, _file(_KEEP, targets=[stored]))
        assert "MEHO applied the file." in first.text, first.text
        changed = {**stored, "host": "h2"}
        added = {"name": "app-02", "product": "linux", "host": "h3"}
        preview = _preview(client, csrf, _file(_KEEP, targets=[changed, added]))
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    notes = re.findall(
        r"<div[^>]*data-settings-console-only>\s*<span>(.*?)</span>", preview.text, re.S
    )
    assert notes == [
        "`spec.tenants[0].targets[0]` changes the target `app-01`. "
        "MEHO accepts this change only from a person in the web console.",
        "`spec.tenants[0].targets[1]` adds the target `app-02`. "
        "MEHO accepts this change only from a person in the web console.",
    ]
    assert 'data-action="settings-apply"' in preview.text


def _break_policy_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(_session: Any, tenant_plan: Any, failed: list[str]) -> None:
        if tenant_plan.policies is not None:
            failed.append(f"`spec.tenants[{tenant_plan.index}].policies` could not be written.")

    monkeypatch.setattr(settings_service, "_apply_policies", _fail)


def test_the_result_says_when_no_part_was_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_tenants()
    _break_policy_writes(monkeypatch)
    client, mock, csrf = _client()
    try:
        applied = _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    assert "MEHO could not write any part of the file. Nothing changed." in applied.text
    assert "already match" not in applied.text
    assert "MEHO applied" not in applied.text
    assert "data-settings-failed" in applied.text
    assert _state() == _UNCHANGED


def test_the_result_says_when_part_was_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_tenants()
    _break_policy_writes(monkeypatch)
    rule = {"slug": "rule-001", "title": "Rule", "body": "Nights only.", "kind": "reference"}
    content = _file({"announce_gate_enabled": False}, conventions=[rule])
    client, mock, csrf = _client()
    try:
        applied = _apply(client, csrf, content)
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    assert "MEHO applied part of the file." in applied.text
    assert "MEHO applied the file." not in applied.text
    assert "data-settings-failed" in applied.text
    assert _state() == (True, False, 1, 1)


@pytest.mark.parametrize("path", [_PREVIEW, _APPLY])
@pytest.mark.parametrize("value", ["not base64!", "Zm9v\u00e9"], ids=["bad-base64", "non-ascii"])
def test_a_post_without_a_file_asks_for_one(path: str, value: str) -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        response = client.post(path, files={"file_b64": (None, value)}, headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    assert "Choose a settings file first." in response.text
    assert _state() == _UNCHANGED


def _audit_rows(op_id: str) -> list[AuditLog]:
    rows = asyncio.run(_all(AuditLog))
    return [row for row in rows if row.payload.get("op_id") == op_id]


def test_one_audit_row_per_apply_with_the_revision_id() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        _preview(client, csrf, _LOOSEN)
        applied = _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert applied.status_code == 200, applied.text
    (row,) = [
        r
        for r in _audit_rows("settings.import")
        if r.event_class == AuditEventClass.OPERATION_EXECUTION
    ]
    (revision,) = asyncio.run(_all(InstanceSettingsRevision))
    assert row.id == revision.audit_id
    assert row.operator_sub == "op-alice"
    assert row.payload["op_class"] == "write"
    assert row.payload["settings_revision_id"] == str(revision.id)
    assert all(r.payload["op_class"] == "read" for r in _audit_rows("settings.import.preview"))


@pytest.mark.usefixtures("sqlite_transactions")
@pytest.mark.parametrize("cutover", [False, True], ids=["cutover-off", "cutover-on"])
def test_a_failed_audit_write_saves_nothing(
    cutover: bool, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if cutover:
        request.getfixturevalue("cutover_on")
    _seed_tenants()

    async def _broken(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("INSERT INTO audit_log", {}, Exception("database is gone"))

    monkeypatch.setattr(settings_routes, "_add_audit_row", _broken)
    client, mock, csrf = _client()
    try:
        applied = _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert applied.status_code == 500, applied.text
    assert "MEHO could not write the audit record of this import." in applied.text
    assert "MEHO applied the file." not in applied.text
    assert _state() == _UNCHANGED
    assert not [
        r
        for r in _audit_rows("settings.import")
        if r.event_class == AuditEventClass.OPERATION_EXECUTION and r.status_code == 200
    ]
