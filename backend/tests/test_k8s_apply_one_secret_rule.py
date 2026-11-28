# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The storage rule of ``k8s.apply`` and the card that hides each value it counts.

Each call goes through ``dispatch`` to the stored approval request, over the
stand-in Kubernetes API server of ``test_k8s_apply_card_through_dispatch``.
Each value is made up, with no word a hide rule knows.

* A value the storage rule counts makes the stored request keep the
  manifest only in the encrypted hand-off: the value is on no card, in no
  view, not in the stored params, not in the audit rows and not in the logs.
* The card hides the argument after ``-p`` after any program. For the
  stored request ``-p`` counts only after a program that takes a password
  there (``mysql``, ``sshpass``): ``mkdir -p /data`` and ``-p 8080`` keep
  their params on the row, also without the key.
* Without the hand-off key, MEHO refuses such an apply at once with a text
  that says what to set.
* The run after approval applies the manifest the caller sent, unchanged.
* The list of what each container runs names the object as the card does,
  and an error keeps its HTTP status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import pytest
import structlog
import yaml
from sqlalchemy import func, select

from meho_backplane.connectors.kubernetes.op_classes import HANDOFF_KEY_REFUSAL
from meho_backplane.connectors.kubernetes.ops_write_preview import names_as_on_card
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import _preview, dispatch
from meho_backplane.operations._preview import preview_failure_reason
from meho_backplane.settings import get_settings
from tests.test_k8s_apply_card_secrets import (
    _approve_and_run,
    _audit_text,
    _digits,
    _everything_shown,
    _made_up,
)
from tests.test_k8s_apply_card_through_dispatch import (
    _CONNECTOR_ID,
    _bff_env,
    _console_card,
    _kubernetes_registry,
    _login,
    _park,
    _refuse,
    _setup,
    _StandInServer,
    _Target,
    _views,
)
from tests.test_k8s_apply_card_values import _HIDDEN_BY_RULE, _hide_by_path
from tests.test_k8s_apply_object_summary import _Response

__all__ = ["_bff_env", "_kubernetes_registry"]


class _RecordingServer(_StandInServer):
    """The stand-in API server that also takes the real apply and keeps each body sent."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.bodies: list[tuple[bool, Any]] = []

    async def server_side_apply(self, resource: Any, **kwargs: Any) -> _Response:
        dry_run = kwargs.get("dry_run") == "All"
        self.bodies.append((dry_run, json.loads(json.dumps(kwargs["body"]))))
        if dry_run:
            return await super().server_side_apply(resource, **kwargs)
        self.requests.append(("apply", resource.kind, kwargs["name"]))
        body = kwargs["body"]
        stored = {**body["metadata"], "resourceVersion": "7", "uid": str(uuid.uuid4())}
        return _Response(200, {**body, "metadata": stored})


def _pod(container: str, *, name: str = "report", namespace: str = "prod") -> str:
    """A Pod alone in the manifest, with one container given in YAML flow style."""
    return (
        f"apiVersion: v1\nkind: Pod\nmetadata: {{name: '{name}', namespace: {namespace}}}\n"
        f"spec: {{containers: [{{name: app, image: report:1, {container}}}]}}\n"
    )


#: Forms of a secret value V. The card hides V, and the stored request keeps
#: the manifest only in the encrypted hand-off.
_SECRET_FORMS: dict[str, str] = {
    "password glued with =": "args: ['--password={v}']",
    "password as the next item": "args: [--password, '{v}']",
    "password as a number": "args: [--password, {d}]",
    "password that starts with a dash": "args: [--password, '-{v}']",
    "mysql -p and the value as two items": "command: [mysql, -u, root, -p, '{v}']",
    "mysql -p glued": "command: [mysql, -uroot, '-p{v}']",
    "mysql inside sh -c": "command: [sh, -c, 'mysql -uroot -p{v} -e status']",
    "PGPASSWORD as an argument": "args: ['PGPASSWORD={v}']",
    "PGPASSWORD inside sh -c": "command: [sh, -c, 'PGPASSWORD={v} psql -h db']",
    "PGPASSWORD in env": "env: [{{name: PGPASSWORD, value: '{v}'}}]",
    "token flag": "args: [--token, '{v}']",
    "user flag with a password": "args: [--user, 'root:{v}']",
    "curl -u glued": "command: [curl, '-uroot:{v}', 'https://example.test/']",
    "-u and user:password as two items": "args: [-u, 'root:{v}']",
    "password in a URL": "args: ['https://app:{v}@example.test/']",
    "bearer header after -H": "command: [curl, -H, 'Authorization: Bearer {v}']",
    "bearer header after --header": "args: [--header, 'Authorization: Bearer {v}']",
    "bearer header glued to --header": "args: ['--header=Authorization: Bearer {v}']",
}


def _custom_kind(value: str) -> str:
    return (
        "apiVersion: example.test/v1\nkind: Widget\nmetadata: {name: w1, namespace: prod}\n"
        f"spec: {{password: {value}}}\n"
    )


def _deployment_with_env(name: str, value: str) -> str:
    return (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: prod}\n"
        "spec: {template: {spec: {containers: [{name: app, image: web:1, "
        f"env: [{{name: {name}, value: '{value}'}}]}}]}}}}}}\n"
    )


def _secret_manifest(form: str, value: str, digits: str) -> str:
    if form == "custom kind with a text password":
        return _custom_kind(f"'{value}'")
    if form == "custom kind with a number password":
        return _custom_kind(digits)
    if form == "API_TOKEN in the env of a Deployment":
        return _deployment_with_env("API_TOKEN", value)
    return _pod(_SECRET_FORMS[form].format(v=value, d=digits))


_ALL_SECRET_FORMS = sorted(
    [
        *_SECRET_FORMS,
        "custom kind with a text password",
        "custom kind with a number password",
        "API_TOKEN in the env of a Deployment",
    ]
)


@pytest.mark.parametrize("form", _ALL_SECRET_FORMS)
def test_a_secret_value_is_on_no_card_and_not_in_the_stored_params(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, form: str
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    caplog.set_level(logging.DEBUG)
    value, digits = _made_up(), _digits()
    manifest = _secret_manifest(form, value, digits)
    secret = digits if "number" in form else value

    with structlog.testing.capture_logs() as logs:
        row = _park(_login("person-token"), manifest)

    assert row.params is None
    assert row.execution_handle is not None
    shown = {
        "stored request, views and console card": _everything_shown(row),
        "audit rows": _audit_text(),
        "structlog": json.dumps(logs, default=str),
        # The test database driver logs each insert at DEBUG; MEHO's own
        # loggers are the ones read here.
        "stdlib log": "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("meho_backplane")
        ),
    }
    for place, text in shown.items():
        assert secret not in text, place


@pytest.mark.parametrize("refused", [frozenset(), frozenset({"Pod"})], ids=["worked", "failed"])
def test_after_a_database_client_the_argument_of_p_is_hidden_on_the_card(
    monkeypatch: pytest.MonkeyPatch, refused: frozenset[str]
) -> None:
    server = _StandInServer(refused_kinds=refused)
    _setup(monkeypatch, server)
    value = _made_up()

    row = _park(_login("person-token"), _pod(f"command: [mysql, -uroot, '-p{value}', -e, status]"))

    effect = row.proposed_effect
    commands = effect["preview"]["run_commands"] if "preview" in effect else effect["run_commands"]
    assert commands[0]["command"] == ["mysql", "-uroot", "-p***REDACTED***", "-e", "status"]
    assert value not in _everything_shown(row)


#: Forms with no secret for the stored request. The call keeps its params on
#: the row, also without the hand-off key. The card hides the argument after
#: ``-p`` after any program.
_H = "***REDACTED***"
_PLAIN_FORMS: dict[str, tuple[str, list[Any]]] = {
    "mkdir -p": ("command: [mkdir, -p, /data]", ["mkdir", "-p", _H]),
    "mkdir -p inside sh -c": (
        "command: [sh, -c, 'mkdir -p /data && exec app']",
        ["sh", "-c", f"mkdir -p {_H} && exec app"],
    ),
    "-p with a number": ("args: [-p, 8080]", ["-p", _H]),
    "-p with a text": ("args: [-p, '8080']", ["-p", _H]),
    "nc -l -p": ("command: [nc, -l, -p, 8080]", ["nc", "-l", "-p", _H]),
    "psql -p": ("command: [psql, -h, db, -p, 5432]", ["psql", "-h", "db", "-p", _H]),
    "ssh -p": ("command: [ssh, -p, 22, host]", ["ssh", "-p", _H, "host"]),
    "-u with a user": ("args: [-u, root]", ["-u", "root"]),
    "redis --port": ("command: [redis-server, --port, 6379]", ["redis-server", "--port", 6379]),
}


@pytest.mark.parametrize("form", sorted(_PLAIN_FORMS))
def test_a_command_line_with_no_secret_shows_and_waits_without_the_key(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    container, line = _PLAIN_FORMS[form]
    manifest = _pod(container)

    row = _park(_login("person-token"), manifest)

    assert row.params == {"manifest": manifest}
    assert row.execution_handle is None
    (entry,) = row.proposed_effect["preview"]["run_commands"]
    assert [*entry.get("command", []), *entry.get("args", [])] == line


def _refused_without_the_key(manifest: str) -> Any:
    async def _do() -> Any:
        return await dispatch(
            operator=_login("person-token"),
            connector_id=_CONNECTOR_ID,
            op_id="k8s.apply",
            target=_Target(),
            params={"manifest": manifest},
        )

    return asyncio.run(_do())


def _stored_requests() -> int:
    async def _count() -> int:
        async with get_sessionmaker()() as session:
            return int((await session.execute(select(func.count(ApprovalRequest.id)))).scalar_one())

    return asyncio.run(_count())


#: Forms with no secret that count by the rule, so they need the hand-off
#: key: the name of a flag, a ``NAME=`` or an ``env`` entry holds a secret word.
_REFUSED_WITHOUT_THE_KEY: dict[str, str] = {
    "--password-file": "args: [--password-file, /run/app/pw]",
    "--token-ttl": "args: [--token-ttl, 60]",
    "--tls-private-key-file": "args: [--tls-private-key-file, /etc/tls/tls.pem]",
    "--authorization-mode": "args: [--authorization-mode, 'Node,RBAC']",
    "--passthrough": "args: [--passthrough, 'on']",
    "--secret-name": "args: [--secret-name, app-secret]",
    "PASSWORD_MIN_LENGTH=12": "args: ['PASSWORD_MIN_LENGTH=12']",
    "--auth-mode=rbac": "args: ['--auth-mode=rbac']",
    "KEYCLOAK_URL=": "args: ['KEYCLOAK_URL=https://sso.example.test']",
    "env PASSWORD_MIN_LENGTH": "env: [{{name: PASSWORD_MIN_LENGTH, value: '12'}}]",
    "env SSH_AUTH_SOCK": "env: [{{name: SSH_AUTH_SOCK, value: /run/agent.sock}}]",
    "--api-key-file": "args: [--api-key-file, /run/app/key]",
    "--password with a value": "args: [--password, '{v}']",
    "mysql -p glued": "command: [mysql, -uroot, '-p{v}']",
    "mysql -p and the value as two items": "command: [mysql, -u, root, -p, '{v}']",
}


@pytest.mark.parametrize("form", sorted(_REFUSED_WITHOUT_THE_KEY))
def test_without_the_key_an_apply_with_a_hidden_value_is_refused_with_what_to_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, form: str
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    caplog.set_level(logging.DEBUG)
    value = _made_up()

    with structlog.testing.capture_logs() as logs:
        result = _refused_without_the_key(_pod(_REFUSED_WITHOUT_THE_KEY[form].format(v=value)))

    assert result.status == "denied"
    assert result.extras["reason"] == HANDOFF_KEY_REFUSAL
    assert _stored_requests() == 0
    for place, text in {
        "answer": json.dumps(result.model_dump(mode="json"), default=str),
        "audit rows": _audit_text(),
        "structlog": json.dumps(logs, default=str),
        "stdlib log": caplog.text,
    }.items():
        assert value not in text, place


def test_the_refusal_text_says_what_to_set() -> None:
    assert HANDOFF_KEY_REFUSAL == (
        "MEHO cannot keep this request: MEHO keeps this manifest only in the encrypted "
        "hand-off, because it holds a secret value or MEHO cannot read it, and the hand-off "
        "needs a key. Set APPROVAL_HANDOFF_ENCRYPTION_KEY on the MEHO server (in the Helm "
        "chart: approvalHandoff), then send the call again."
    )


def test_an_unreadable_manifest_is_refused_before_the_wait_with_and_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\ndata: {a: *nowhere}\n"
    expected = (
        "The manifest cannot be applied: document 1 is not valid YAML or JSON "
        "(an alias or anchor YAML refuses, line 4, column 11). "
    )

    assert _refuse(_login("person-token"), manifest).extras["reason"].startswith(expected)

    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()
    result = _refuse(_login("person-token"), manifest)
    assert result.extras["reason"].startswith(expected)
    assert server.requests == []


def test_without_the_key_a_secret_create_keeps_its_own_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)
    monkeypatch.delenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", raising=False)
    get_settings.cache_clear()

    async def _do() -> Any:
        return await dispatch(
            operator=_login("person-token"),
            connector_id=_CONNECTOR_ID,
            op_id="k8s.secret.create",
            target=_Target(),
            params={"name": "s", "namespace": "prod", "string_data": {"a": "b"}},
        )

    result = asyncio.run(_do())

    assert result.status == "denied"
    assert result.extras["reason"].startswith("credential-write approval custody is unavailable")


@pytest.mark.parametrize(
    "container",
    [
        "args: [--password, '{v}']",
        "command: [mysql, -uroot, '-p{v}']",
        "args: [-u, 'root:{v}']",
        "command: [mkdir, -p, /data]",
    ],
    ids=["password", "mysql", "user and password", "mkdir"],
)
def test_the_run_after_approval_applies_the_manifest_the_caller_sent(
    monkeypatch: pytest.MonkeyPatch, container: str
) -> None:
    server = _RecordingServer()
    _setup(monkeypatch, server)
    manifest = _pod(container.format(v=_made_up()))

    row = _park(_login("person-token"), manifest)
    run = _approve_and_run(monkeypatch, row)

    assert run.status == "ok", run
    (dry_body,) = [body for dry, body in server.bodies if dry]
    (real_body,) = [body for dry, body in server.bodies if not dry]
    assert real_body == dry_body == yaml.safe_load(manifest)


# ---------------------------------------------------------------------------
# The object label and the HTTP status of a failed dry run
# ---------------------------------------------------------------------------


def _with_the_name_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _preview.apply_connector_boundary_redaction

    def _hide_names(raw: Any, **labels: Any) -> Any:
        result = real(raw, **labels)
        return result.model_copy(update={"redacted": _hide_by_path(result.redacted, "")})

    monkeypatch.setattr(_preview, "apply_connector_boundary_redaction", _hide_names)


def test_a_refused_pod_names_its_object_in_the_command_list_as_the_card_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(refused_kinds=frozenset({"Pod"}))
    _setup(monkeypatch, server)
    _with_the_name_rule(monkeypatch)
    name = "ab" + _made_up()

    row = _park(_login("person-token"), _pod("args: [--port, 8443]", name=name))

    effect = row.proposed_effect
    assert effect["preview_unavailable"] is True
    (entry,) = effect["run_commands"]
    assert entry["object"] == f"Pod/prod/{_HIDDEN_BY_RULE}"
    assert name not in json.dumps(effect)
    assert name not in _console_card(row)


def test_a_short_hidden_name_keeps_the_http_status_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _StandInServer(missing_namespaces=frozenset({"nope"}))
    _setup(monkeypatch, server)
    _with_the_name_rule(monkeypatch)

    row = _park(_login("person-token"), _pod("args: [--port, 8443]", name="4", namespace="nope"))

    error = row.proposed_effect["preview_error"]
    assert f"v1/Pod nope/{_HIDDEN_BY_RULE} (HTTP 404 " in error
    assert error.count(_HIDDEN_BY_RULE) == 1
    for view_name, view in _views(row).items():
        assert view["preview_error"] == error, view_name


def test_a_hidden_name_is_replaced_only_where_no_letter_or_digit_touches_it() -> None:
    manifest = "apiVersion: v1\nkind: Pod\nmetadata: {name: '4', namespace: web}\n"

    shown = names_as_on_card(
        manifest,
        'pod 4 refused (HTTP 404 Not Found): port 8443 of pods "4" is taken',
        redact=lambda docs: _hide_by_path(docs, "manifest"),
    )

    assert shown == (
        f"pod {_HIDDEN_BY_RULE} refused (HTTP 404 Not Found): "
        f'port 8443 of pods "{_HIDDEN_BY_RULE}" is taken'
    )


def test_a_hidden_name_that_reads_like_the_status_keeps_the_http_status() -> None:
    manifest = "apiVersion: v1\nkind: Pod\nmetadata: {name: '404', namespace: web}\n"

    shown = names_as_on_card(
        manifest,
        'the API server rejected pods "404" (HTTP 404 Not Found)',
        redact=lambda docs: _hide_by_path(docs, "manifest"),
    )

    assert shown == f'the API server rejected pods "{_HIDDEN_BY_RULE}" (HTTP 404 Not Found)'


# ---------------------------------------------------------------------------
# The stored error and the parser's diagnostics
# ---------------------------------------------------------------------------


def test_a_null_character_in_a_refusal_is_stored_escaped() -> None:
    reason = preview_failure_reason(RuntimeError("admission check refused 'a\x00b'"))

    assert "\x00" not in reason
    assert reason == "RuntimeError: admission check refused 'a\\0b'"


def test_the_agent_reads_the_fixed_phrase_of_meho(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)

    result = _refuse(_login("person-token"), "%YAML 1\n---\na: b\n")

    assert result.extras["reason"].startswith(
        "The manifest cannot be applied: document 1 is not valid YAML or JSON "
        "(a token YAML refuses, line 1, column 8). "
    )
    assert "expected" not in result.extras["reason"]


@pytest.mark.parametrize("escape", ["%FF", "%C3%28"])
def test_a_tag_escape_that_is_not_utf8_names_no_byte(
    monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    server = _StandInServer()
    _setup(monkeypatch, server)

    result = _refuse(_login("person-token"), f"a: !<tag:{escape}> x\n")

    reason = result.extras["reason"]
    assert reason.startswith(
        "The manifest cannot be applied: document 1 is not valid YAML or JSON "
        "(a token YAML refuses, line 1, column 10). "
    )
    assert "0x" not in reason.lower()
    assert escape not in reason
