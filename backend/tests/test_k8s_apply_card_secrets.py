# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret values of a ``k8s.apply`` manifest, through the real dispatch.

Each call goes through ``dispatch`` to the stored approval request, over the
stand-in Kubernetes API server of ``test_k8s_apply_card_through_dispatch``.
Each value is made up, with no word a hide rule knows.

* A manifest MEHO cannot read: the error names the kind of problem and its
  place, and quotes no text of the manifest. That holds in the stored
  request, the console card, the REST and MCP views, the agent's own
  preview, the audit rows, the logs and the answer of the run after a
  second person approves. The call still waits for approval.
* A value under a secret name is hidden whatever type YAML gives it, on a
  card whose dry run worked and on a failed one. A value under an ordinary
  name still shows.
* A password on a command line, alone in the manifest, sends the manifest to
  the encrypted hand-off: the row keeps no params.
* A null character in a quoted text is shown as ``\\0``, so the request can
  be stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _agent,
    _bff_env,
    _console_card,
    _kubernetes_registry,
    _login,
    _park,
    _setup,
    _StandInServer,
    _Target,
    _views,
)

__all__ = ["_bff_env", "_kubernetes_registry"]

_HIDDEN = "[hidden]"

#: The card hides a value with one of these: its own mark or the shared redaction's.
_HIDDEN_MARKS = (_HIDDEN, "***REDACTED***")


def _made_up() -> str:
    """Two letters, 20 hex characters and one letter: no word a hide rule knows."""
    return "qv" + secrets.token_hex(10) + "x"


def _digits() -> str:
    return str(secrets.randbelow(9 * 10**11) + 10**11)


def _row_text(row: ApprovalRequest) -> str:
    return json.dumps([row.params, row.proposed_effect], default=str)


def _audit_text() -> str:
    async def _rows() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(AuditLog))).scalars())

    columns = AuditLog.__table__.columns
    return json.dumps(
        [
            {column.name: getattr(row, column.key) for column in columns}
            for row in asyncio.run(_rows())
        ],
        default=str,
    )


def _agent_preview(manifest: str) -> dict[str, Any]:
    return asyncio.run(
        preview_dispatch(
            operator=_agent(),
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": manifest},
        )
    )


def _approve_and_run(monkeypatch: pytest.MonkeyPatch, row: ApprovalRequest) -> Any:
    """A second person approves *row*; return the answer of the run."""
    approver = _login("approver", role=TenantRole.TENANT_ADMIN)
    target = _Target(id=row.target_id)

    async def _same_target(_session: Any, _tenant: Any, _target_id: Any) -> _Target:
        return target

    monkeypatch.setattr(
        "meho_backplane.targets.resolver.resolve_target_by_id", _same_target, raising=True
    )

    async def _do() -> Any:
        async with get_sessionmaker()() as session:
            await approve_request(session, row.id, operator=approver)
            await session.commit()
        async with get_sessionmaker()() as session:
            stored = await session.get(ApprovalRequest, row.id)
            assert stored is not None
            return await resume_dispatch_after_approval(operator=approver, request=stored)

    return asyncio.run(_do())


def _everything_shown(row: ApprovalRequest) -> str:
    """The stored request, the console card and each view of it, as one text."""
    return _row_text(row) + json.dumps(_views(row), default=str) + _console_card(row)


# ---------------------------------------------------------------------------
# A manifest MEHO cannot read
# ---------------------------------------------------------------------------

#: Each form puts the value V in a place where the parser names it.
_UNREADABLE_FORMS = {
    "undefined alias": "*{v}",
    "unknown tag with a value": "!{v} x",
    "unknown tag alone": "!{v}",
    "verbatim tag": "!<tag:{v}> y",
    "undefined tag handle": "!{v}!x y",
    "python name tag": "!!python/name:{v} ''",
    "int tag": "!!int {v}",
    "float tag": "!!float {v}",
    "bool tag": "!!bool {v}",
}


def _secret_with(value: str) -> str:
    return (
        "apiVersion: v1\nkind: Secret\nmetadata: {name: s, namespace: prod}\n"
        f"stringData:\n  password: {value}\n"
    )


def _assert_unreadable_manifest_hidden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, manifest: str, value: str
) -> str:
    """Park, preview and run *manifest*; check *value* is nowhere. Return the stored error."""
    server = _StandInServer()
    _setup(monkeypatch, server)
    caplog.set_level(logging.DEBUG)

    with structlog.testing.capture_logs() as logs:
        row = _park(_login("person-token"), manifest)
        preview = _agent_preview(manifest)
        run = _approve_and_run(monkeypatch, row)

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    error = effect["preview_error"]
    assert error.startswith("ApplyManifestError: manifest is not valid YAML/JSON")
    for name, view in _views(row).items():
        assert view["preview_error"] == error, name
    assert server.requests == []
    assert preview["status"] == "ok"
    assert preview["requires_approval"] is True
    shown = {
        "stored request and views": _everything_shown(row),
        # The agent's own manifest comes back to it in ``redacted_body``.
        "agent preview": json.dumps({**preview, "redacted_body": None}, default=str),
        "audit rows": _audit_text(),
        "structlog": json.dumps(logs, default=str),
        "stdlib log": caplog.text,
        "run answer": json.dumps(run.model_dump(mode="json"), default=str),
    }
    for place, text in shown.items():
        assert value not in text, place
    assert "manifest is not valid YAML/JSON" in shown["run answer"]
    return str(error)


@pytest.mark.parametrize("form", sorted(_UNREADABLE_FORMS))
def test_a_parser_error_quotes_no_value_anywhere(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, form: str
) -> None:
    value = _made_up()
    manifest = _secret_with(_UNREADABLE_FORMS[form].format(v=value))

    error = _assert_unreadable_manifest_hidden(monkeypatch, caplog, manifest, value)

    assert "(line 5, column 13)" in error


def test_a_duplicate_tag_handle_quotes_no_value_anywhere(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    value = _made_up()
    manifest = f"%TAG !{value}! tag:a,2000:\n%TAG !{value}! tag:b,2000:\n---\n" + _secret_with("x")

    error = _assert_unreadable_manifest_hidden(monkeypatch, caplog, manifest, value)

    assert "duplicate tag handle '…' (line 2, column 1)" in error


def test_a_control_character_tells_the_agent_the_reason_and_the_position(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    value = _made_up()
    manifest = _secret_with(f"{value}\x07")

    error = _assert_unreadable_manifest_hidden(monkeypatch, caplog, manifest, value)

    position = manifest.index("\x07")
    assert error == (
        "ApplyManifestError: manifest is not valid YAML/JSON: unacceptable character: "
        f"special characters are not allowed (position {position})"
    )


# ---------------------------------------------------------------------------
# A value under a secret name, whatever its type
# ---------------------------------------------------------------------------


def _config_and_pod(password: str, flag_value: str) -> str:
    return (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
        f"data: {{password: {password}, apiPassword: true, port: 8443}}\n"
        "---\n"
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        "spec:\n  automountServiceAccountToken: false\n"
        "  containers:\n  - {name: app, image: report:1,\n"
        f"     args: [--password, {flag_value}, --port, 8443]}}\n"
    )


def _manifest_of(effect: dict[str, Any]) -> list[dict[str, Any]]:
    return effect["preview"]["manifest"] if "preview" in effect else effect["manifest"]


@pytest.mark.parametrize("refused", [frozenset(), frozenset({"Pod"})], ids=["worked", "failed"])
def test_a_number_under_a_secret_name_is_hidden(
    monkeypatch: pytest.MonkeyPatch, refused: frozenset[str]
) -> None:
    server = _StandInServer(refused_kinds=refused)
    _setup(monkeypatch, server)
    password, flag_value = _digits(), _digits()

    row = _park(_login("person-token"), _config_and_pod(password, flag_value))

    effect = row.proposed_effect
    assert bool(effect.get("preview_unavailable")) is bool(refused)
    config, pod = _manifest_of(effect)
    assert config["data"]["port"] == 8443
    for key in ("password", "apiPassword"):
        assert config["data"][key] in _HIDDEN_MARKS, key
    assert pod["spec"]["automountServiceAccountToken"] is False
    commands = effect["preview"]["run_commands"] if "preview" in effect else effect["run_commands"]
    flag, hidden, *rest = commands[0]["args"]
    assert (flag, rest) == ("--password", ["--port", 8443])
    assert hidden in _HIDDEN_MARKS
    shown = _everything_shown(row)
    for value in (password, flag_value):
        assert value not in shown
    if not refused:
        # A failed preview shows the manifest to a person only.
        assert "8443" in json.dumps(_views(row)["rest-agent"])


@pytest.mark.parametrize("flag_value", ["digits", "text"])
@pytest.mark.parametrize("refused", [frozenset(), frozenset({"Pod"})], ids=["worked", "failed"])
def test_a_password_on_a_command_line_goes_to_the_encrypted_hand_off(
    monkeypatch: pytest.MonkeyPatch, refused: frozenset[str], flag_value: str
) -> None:
    server = _StandInServer(refused_kinds=refused)
    _setup(monkeypatch, server)
    value = _digits() if flag_value == "digits" else _made_up()
    written = value if flag_value == "digits" else f"'{value}'"
    manifest = (
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        f"spec: {{containers: [{{name: app, image: report:1, args: [--password, {written}]}}]}}\n"
    )

    row = _park(_login("person-token"), manifest)

    assert row.params is None
    assert row.execution_handle is not None
    assert value not in _everything_shown(row)


def test_a_pod_with_no_secret_keeps_its_params_on_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    manifest = (
        "apiVersion: v1\nkind: Pod\nmetadata: {name: report, namespace: prod}\n"
        "spec: {containers: [{name: app, image: report:1, args: [--port, 8443, --verbose]}]}\n"
    )

    row = _park(_login("person-token"), manifest)

    assert row.params == {"manifest": manifest}


# ---------------------------------------------------------------------------
# A null character in a quoted text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refused", [frozenset(), frozenset({"ConfigMap"})], ids=["worked", "failed"]
)
def test_a_null_character_is_shown_escaped_and_the_request_is_stored(
    monkeypatch: pytest.MonkeyPatch, refused: frozenset[str]
) -> None:
    server = _StandInServer(refused_kinds=refused)
    _setup(monkeypatch, server)
    manifest = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings, namespace: prod}\n"
        'data: {note: "a\\0b"}\n'
    )

    row = _park(_login("person-token"), manifest)

    (config,) = _manifest_of(row.proposed_effect)
    assert config["data"] == {"note": "a\\0b"}
    # PostgreSQL refuses a null character in a JSON column.
    assert "\\u0000" not in json.dumps([row.params, row.proposed_effect])
    card = _console_card(row)
    # The card shows the text as JSON, which writes the backslash twice.
    assert '"note": "a\\\\0b"' in card
    assert "\x00" not in card
