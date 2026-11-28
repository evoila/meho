# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit rows and answers of a settings import that stops, and the page edges.

* Every import that stops leaves one audit row that names the attempt (the
  keys of ``ATTEMPT_AUDIT_KEYS``) and no change, on the console page and on
  REST, in both audit modes.
* An apply that is cancelled while MEHO checks the file names the operation
  and the file hash.
* An apply refused for a missing reason keeps the preview and its Apply form.
* A person without the admin role gets a console error page (HTML), not JSON.
* A file over 4 MiB, the CSRF check of the preview and the audit class of the
  preview.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.models import InstanceSettingsRevision
from meho_backplane.instance_settings import service as settings_service
from meho_backplane.instance_settings.attempt_audit import ATTEMPT_AUDIT_KEYS
from meho_backplane.instance_settings.schema import MAX_FILE_BYTES

from .test_ui_settings_file import (
    _APPLY,
    _LOOSEN,
    _PAGE,
    _PREVIEW,
    _TENANT_A,
    _UNCHANGED,
    _apply,
    _apply_field,
    _audit_rows,
    _audited_app,  # noqa: F401  (autouse fixture)
    _bff_env,  # noqa: F401  (autouse fixture)
    _client,
    _csrf_headers,
    _preview,
    _seed_tenants,
    _state,
    cutover_on,  # noqa: F401  (fixture)
)

#: What the audit middleware adds to every row; the rest is the attempt.
_MIDDLEWARE_KEYS = {"broadcast_detail_origin", "broadcast_detail_effective", "client"}
_ATTEMPT_PAYLOAD_KEYS = {key.removeprefix("audit_") for key in ATTEMPT_AUDIT_KEYS}
_CUTOVER = pytest.mark.parametrize("cutover", [False, True], ids=["cutover-off", "cutover-on"])


@_CUTOVER
def test_a_failed_console_apply_keeps_exactly_the_attempt_keys(
    cutover: bool, request: pytest.FixtureRequest
) -> None:
    if cutover:
        request.getfixturevalue("cutover_on")
    _seed_tenants()

    def _broken(*_args: Any) -> None:
        raise OperationalError("INSERT", {}, Exception("database is gone"))

    client, mock, csrf = _client()
    event.listen(InstanceSettingsRevision, "before_insert", _broken)
    try:
        applied = _apply(client, csrf, _LOOSEN)
    finally:
        event.remove(InstanceSettingsRevision, "before_insert", _broken)
        mock.stop()

    assert applied.status_code == 500, applied.text
    assert _state() == _UNCHANGED
    (row,) = _audit_rows("settings.import")
    assert set(row.payload) - _MIDDLEWARE_KEYS == _ATTEMPT_PAYLOAD_KEYS
    assert row.payload["op_class"] == "write"
    assert row.payload["settings_tenant_ids"] == [str(_TENANT_A)]


@_CUTOVER
def test_an_apply_cancelled_while_the_file_is_checked_names_the_attempt(
    cutover: bool, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if cutover:
        request.getfixturevalue("cutover_on")
    _seed_tenants()

    async def _cancel(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(settings_service, "build_plan", _cancel)
    client, mock, csrf = _client()
    try:
        with pytest.raises(concurrent.futures.CancelledError):
            _apply(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert _state() == _UNCHANGED
    (row,) = _audit_rows("settings.import")
    # MEHO has not read the tenants yet, so the row names no tenant.
    assert set(row.payload) - _MIDDLEWARE_KEYS == _ATTEMPT_PAYLOAD_KEYS - {"settings_tenant_ids"}
    assert row.payload["settings_import"] is True
    assert row.payload["settings_file_sha256"] == hashlib.sha256(_LOOSEN).hexdigest()
    assert row.payload["op_class"] == "read"


def test_a_missing_reason_keeps_the_preview_and_the_same_file() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        refused = _apply(client, csrf, _LOOSEN, "   ")
        encoded = _apply_field(refused.text)
        again = client.post(
            _APPLY,
            files={"file_b64": (None, encoded), "reason": (None, "Planned maintenance")},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()

    assert refused.status_code == 403, refused.text
    assert "needs a reason. Show the preview again and fill in the Reason field." in refused.text
    assert "data-settings-refused" in refused.text
    assert "data-settings-preview" in refused.text
    assert 'data-action="settings-apply"' in refused.text
    assert 'name="reason"' in refused.text
    assert base64.b64decode(encoded) == _LOOSEN
    assert again.status_code == 200, again.text
    assert _state() == (False, True, 0, 1)


def test_a_missing_reason_writes_nothing() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        _apply(client, csrf, _LOOSEN, None)
    finally:
        mock.stop()
    assert _state() == _UNCHANGED
    assert [row.status_code for row in _audit_rows("settings.import")] == [403]


@pytest.mark.parametrize("role", [TenantRole.OPERATOR.value, TenantRole.READ_ONLY.value])
def test_a_person_without_an_admin_role_gets_a_console_error_page(role: str) -> None:
    _seed_tenants()
    client, mock, csrf = _client(role=role)
    try:
        response = client.get(_PAGE, headers=_csrf_headers(csrf))
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "data-settings-forbidden" in response.text
    assert "data-settings-form" not in response.text


def test_a_file_over_4_mib_is_refused_on_the_page() -> None:
    _seed_tenants()
    big = b"#" * (MAX_FILE_BYTES + 1)
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, big)
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    assert "The file is larger than 4 MiB." in preview.text
    assert 'data-action="settings-apply"' not in preview.text
    assert _state() == _UNCHANGED


def test_the_preview_needs_the_csrf_token() -> None:
    _seed_tenants()
    client, mock, _csrf = _client(with_csrf=False)
    try:
        response = client.post(
            _PREVIEW,
            files={"file": ("meho-settings.yaml", _LOOSEN, "application/yaml")},
            headers={"HX-Request": "true"},
        )
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert _state() == _UNCHANGED
    assert _audit_rows("settings.import.preview") == []


def test_a_preview_writes_no_operation_row_after_the_cutover(
    request: pytest.FixtureRequest,
) -> None:
    request.getfixturevalue("cutover_on")
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        preview = _preview(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    assert preview.status_code == 200, preview.text
    assert _audit_rows("settings.import.preview") == []


def test_a_preview_leaves_a_read_row_before_the_cutover() -> None:
    _seed_tenants()
    client, mock, csrf = _client()
    try:
        _preview(client, csrf, _LOOSEN)
    finally:
        mock.stop()

    (row,) = _audit_rows("settings.import.preview")
    assert row.payload["op_class"] == "read"
