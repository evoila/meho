# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Orchestrator reads: version, health, workflows, workflow runs and their logs.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.vro.connector.VroConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.vro.read_ops`.

Every call goes through the connector's login and asks for JSON
(``Accept: application/json``; the API answers XML by default). A ``401``
is raised as :class:`httpx.HTTPStatusError`, so the dispatcher drops the
login with ``invalidate_session`` and runs the read once more.

**Flat rows.** The Orchestrator answers a list as ``link[]`` objects whose
fields sit in an ``attributes`` list of ``{name, value}`` pairs. A list
read turns each link into one flat row (``{<name>: <value>, ..., href}``)
and returns ``{"items": [...]}``, so MEHO can store a long list behind a
result handle and the agent can filter it with ``result_query``.

**Secrets.** A workflow run's inputs and outputs can hold passwords. A
parameter whose type is ``SecureString``, whose ``encrypt-value`` flag is
set, or whose name is a secret name comes back with its value replaced by
:data:`REDACTED`. A name is secret when MEHO's shared rule
(:func:`~meho_backplane.broadcast.events.is_secret_param_name`: password,
passphrase, secret, token, API key, private key, ...) says so, or when it
is one of the names listed here that the shared rule leaves out (bare
``license``, ``authorization``). Any field with such a name, at any depth,
is replaced too. The connector masks the same fields before the flight
recorder records a vendor answer.

**Checksum.** ``vro.workflow.get`` returns ``content_sha256``: the SHA-256
of the workflow content in a fixed form (JSON with sorted keys and no
spaces), so the same content always gives the same checksum.

**Outcome.** ``vro.execution.get`` returns ``outcome`` next to the vendor
``state``: a plain result from :data:`OUTCOME_BY_STATE`. A state that is
not in the table gives ``unknown``. When the server does not answer, the
read returns ``outcome: not_reachable`` with the run id and the time. Two
errors are not "not reachable" and go on to the dispatcher: a server that
answers with a certificate MEHO does not trust (the dispatcher names the
host and how to fix the trust), and a destination MEHO itself refused
before any request, because it resolves to a private address.

**More rows.** Every list read returns ``more_rows``: it asks the server
for one row more than it returns, and the extra row only tells whether
more rows exist.

**Log text.** A log line's text is cut at :data:`LOG_TEXT_MAX` characters.
The shared secret rules run on the whole text first: they need the label
and the whole value, and a cut could leave a fragment too short for them
to find later.

The paths come from the vendor's Orchestrator REST API reference (9.0 and
9.1); the reconcile lane checks every path in :data:`VENDOR_CALLS`.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors._shared.api_file_read import text_redactor
from meho_backplane.connectors._shared.api_file_values import TextRule
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.vro.session import VroTargetLike
from meho_backplane.operations._rfc6570 import has_dot_segment

if TYPE_CHECKING:
    from meho_backplane.connectors.vro.connector import VroConnector

__all__ = [
    "ABOUT_PATH",
    "DEFAULT_LIST_LIMIT",
    "LOG_TEXT_MAX",
    "OUTCOME_BY_STATE",
    "REDACTED",
    "VENDOR_CALLS",
    "content_sha256",
    "flat_rows",
    "redact_secrets",
    "vro_about_impl",
    "vro_execution_get_impl",
    "vro_execution_list_impl",
    "vro_execution_logs_impl",
    "vro_health_impl",
    "vro_workflow_get_impl",
    "vro_workflow_list_impl",
]

ABOUT_PATH: Final = "/vco/api/about"
_HEALTH_PATH: Final = "/vco/api/healthstatus"
_WORKFLOWS_PATH: Final = "/vco/api/workflows"
_WORKFLOW_PATH: Final = "/vco/api/workflows/{workflowId}"
_WORKFLOW_CONTENT_PATH: Final = "/vco/api/workflows/{workflowId}/content"
_EXECUTIONS_PATH: Final = "/vco/api/workflows/{workflowId}/executions"
_EXECUTION_PATH: Final = "/vco/api/workflows/{workflowId}/executions/{executionId}"
_SYSLOGS_PATH: Final = "/vco/api/workflows/{workflowId}/executions/{executionId}/syslogs"

#: Every vendor call the reads make, as ``(method, path template)``.
#: The reconcile lane checks each one against the pinned reference lists.
VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", ABOUT_PATH),
        ("GET", _HEALTH_PATH),
        ("GET", _WORKFLOWS_PATH),
        ("GET", _WORKFLOW_PATH),
        ("GET", _WORKFLOW_CONTENT_PATH),
        ("GET", _EXECUTIONS_PATH),
        ("GET", _EXECUTION_PATH),
        ("GET", _SYSLOGS_PATH),
    }
)

#: Rows a list read asks for when the caller sets no ``max_results``.
DEFAULT_LIST_LIMIT: Final = 500
#: Log lines ``vro.execution.logs`` asks for when the caller sets no ``max_lines``.
_DEFAULT_LOG_LINES: Final = 200
#: Longest log text kept per line; a longer text is cut and the row is marked.
LOG_TEXT_MAX: Final = 2000
_LOGS_OP_ID: Final = "vro.execution.logs"

#: The plain outcome of each run state of the vendor reference
#: (canceled, completed, running, suspended, waiting, waiting-signal,
#: failed, initializing). A suspended run waits for someone to resume it.
OUTCOME_BY_STATE: Final[dict[str, str]] = {
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "running": "running",
    "initializing": "running",
    "waiting": "waiting",
    "waiting-signal": "waiting",
    "suspended": "waiting",
}
_OUTCOME_UNKNOWN: Final = "unknown"
_OUTCOME_NOT_REACHABLE: Final = "not_reachable"

#: Written in place of a secret value; the key stays, so the shape is stable.
REDACTED: Final = "***REDACTED***"

#: Key names (lower case, without ``_`` and ``-``) whose value is secret.
#: Checked next to the shared rule, which has no bare ``license`` or
#: ``authorization``.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "clientsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "authorization",
        "apikey",
        "secretkey",
        "privatekey",
        "licensekey",
        "licencekey",
        "license",
        "licence",
    }
)
#: A key that ends with one of these (``adminPassword``, ``apiToken``) is secret too.
_SECRET_SUFFIXES: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "secretkey",
    "privatekey",
    "licensekey",
    "licencekey",
)


def _is_secret_name(key: object) -> bool:
    """True when *key* names a secret: by the shared rule or by the lists above."""
    if not isinstance(key, str):
        return False
    if is_secret_param_name(key):
        return True
    name = key.lower().replace("_", "").replace("-", "")
    return name in _SECRET_KEYS or name.endswith(_SECRET_SUFFIXES)


def _is_secret_parameter(param: dict[str, Any]) -> bool:
    """A workflow parameter whose value must not come back."""
    kind = param.get("type")
    if isinstance(kind, str) and "securestring" in kind.lower():
        return True
    if param.get("encrypt-value") is True:
        return True
    return _is_secret_name(param.get("name"))


def _redact_object(value: dict[str, Any]) -> dict[str, Any]:
    """:func:`redact_secrets` for one object."""
    redacted: dict[str, Any] = redact_secrets(value)
    return redacted


def redact_secrets(value: Any) -> Any:
    """Return *value* with every secret field and secret parameter value replaced. No mutation."""
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if not isinstance(value, dict):
        return value
    # Parameters are ``{name, type, value}``; Properties entries are ``{key, value}``.
    secret_param = "value" in value and (
        ("name" in value and _is_secret_parameter(value)) or _is_secret_name(value.get("key"))
    )
    return {
        key: (
            REDACTED
            if _is_secret_name(key) or (secret_param and key == "value")
            else redact_secrets(item)
        )
        for key, item in value.items()
    }


def _segment(kind: str, value: Any) -> str:
    """Return *value* as one URL path segment, or raise ``ValueError``."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"the {kind} id must be a non-empty string, got {value!r}")
    if value == "." or has_dot_segment(value):
        raise ValueError(f"the {kind} id {value!r} must not be '.' or contain a '..' segment")
    return quote(value, safe="")


def flat_rows(links: Any) -> list[dict[str, Any]]:
    """Turn the vendor ``link[]`` objects into flat rows.

    Each ``attributes`` entry ``{name, value}`` becomes one field of the
    row; ``href`` is kept. A link without attributes is skipped.
    """
    if not isinstance(links, list):
        return []
    rows: list[dict[str, Any]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        attributes = link.get("attributes")
        if not isinstance(attributes, list) or not attributes:
            continue
        row: dict[str, Any] = {}
        for attribute in attributes:
            if isinstance(attribute, dict) and isinstance(attribute.get("name"), str):
                row[attribute["name"]] = attribute.get("value")
        row["href"] = link.get("href")
        rows.append(row)
    return rows


def _max_results(params: dict[str, Any]) -> int:
    return int(params.get("max_results", DEFAULT_LIST_LIMIT))


def _page_query(params: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``maxResult`` / ``startIndex`` query of a list read.

    Asks for one row more than ``max_results``; :func:`_page_answer` drops
    it and sets ``more_rows``.
    """
    return {
        "maxResult": _max_results(params) + 1,
        "startIndex": int(params.get("start_index", 0)),
    }


def _page_answer(envelope: Any, params: dict[str, Any]) -> dict[str, Any]:
    """``{items, total, start_index, count, more_rows}`` from a vendor list envelope."""
    page = envelope if isinstance(envelope, dict) else {}
    links = page.get("link")
    links = links if isinstance(links, list) else []
    limit = _max_results(params)
    rows = flat_rows(links[:limit])
    return {
        "items": redact_secrets(rows),
        "total": page.get("total"),
        "start_index": page.get("startIndex"),
        "count": len(rows),
        "more_rows": len(links) > limit,
    }


async def vro_about_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.about`` -- ``GET /vco/api/about``: version, build and API version."""
    del params
    payload = await connector._vro_get(target, ABOUT_PATH, operator=operator)
    zone = payload.get("time-zone")
    features = payload.get("features")
    return {
        "version": payload.get("version"),
        "build_number": payload.get("build-number"),
        "build_date": payload.get("build-date"),
        "api_version": payload.get("api-version"),
        "id": payload.get("id"),
        "time_zone": zone.get("id") if isinstance(zone, dict) else None,
        "features": [
            feature["name"]
            for feature in features or []
            if isinstance(feature, dict) and isinstance(feature.get("name"), str)
        ],
    }


def _provider_row(provider: Any) -> dict[str, Any]:
    item = provider if isinstance(provider, dict) else {}
    return {
        "name": item.get("name"),
        "state": item.get("state"),
        "critical": item.get("critical"),
        "message": item.get("message"),
        # The vendor schema spells the field "descritpion"; read both spellings.
        "description": item.get("description", item.get("descritpion")),
    }


async def vro_health_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.health`` -- ``GET /vco/api/healthstatus?showDetails=true``."""
    del params
    payload = await connector._vro_get(
        target, _HEALTH_PATH, operator=operator, params={"showDetails": "true"}
    )
    health = payload.get("health-status")
    health = health if isinstance(health, dict) else {}
    providers = health.get("health-status-providers")
    return _redact_object(
        {
            "server_state": payload.get("serverState"),
            "health_status": health.get("healthStatus"),
            "description": health.get("description"),
            "time": health.get("time"),
            "instance_id": payload.get("instance-id"),
            "pending_restart_in": payload.get("pending-restart-in"),
            "providers": [_provider_row(p) for p in providers or [] if isinstance(p, dict)],
        }
    )


async def vro_workflow_list_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.workflow.list`` -- ``GET /vco/api/workflows``, one flat row per workflow.

    ``name`` becomes the vendor condition ``name~<text>`` (name contains
    the text), ``category`` becomes ``categoryName=<name>``.
    """
    query: dict[str, Any] = _page_query(params)
    conditions: list[str] = []
    if params.get("name"):
        conditions.append(f"name~{params['name']}")
    if params.get("category"):
        conditions.append(f"categoryName={params['category']}")
    if conditions:
        query["conditions"] = conditions
    payload = await connector._vro_get(target, _WORKFLOWS_PATH, operator=operator, params=query)
    return _page_answer(payload, params)


def _declared_parameters(declaration: Any) -> list[dict[str, Any]]:
    """``{name, type, description}`` of each declared input or output, with no values."""
    params = declaration.get("param") if isinstance(declaration, dict) else declaration
    if not isinstance(params, list):
        return []
    return [
        {"name": p.get("name"), "type": p.get("type"), "description": p.get("description")}
        for p in params
        if isinstance(p, dict)
    ]


def _with_secret_flag(params: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``secret``: true for a ``SecureString`` parameter, false otherwise."""
    return [
        {**p, "secret": isinstance(p["type"], str) and "securestring" in p["type"].lower()}
        for p in params
    ]


def content_sha256(content: Any) -> str:
    """SHA-256 of *content* in a fixed form: JSON with sorted keys and no spaces."""
    fixed = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(fixed.encode("utf-8")).hexdigest()


async def vro_workflow_get_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.workflow.get`` -- the workflow and its declared inputs and outputs.

    ``GET /vco/api/workflows/{workflowId}`` gives the name, version,
    description and category; ``GET .../content`` gives the declared
    inputs and outputs, and ``content_sha256`` is the checksum of that
    content. Each input has ``secret`` (true for a ``SecureString``).
    Default values are not returned: they can hold secrets and are not
    needed to call the workflow.
    """
    workflow_id = _segment("workflow", params.get("workflow_id"))
    workflow = await connector._vro_get(
        target, _WORKFLOW_PATH.format(workflowId=workflow_id), operator=operator
    )
    content = await connector._vro_get(
        target, _WORKFLOW_CONTENT_PATH.format(workflowId=workflow_id), operator=operator
    )
    inputs = _declared_parameters(content.get("input"))
    outputs = _declared_parameters(content.get("output"))
    return {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "display_name": content.get("display-name"),
        "version": workflow.get("version"),
        "description": workflow.get("description"),
        "category_id": workflow.get("category-id"),
        "content_sha256": content_sha256(content),
        "inputs": _with_secret_flag(
            inputs or _declared_parameters(workflow.get("inputParameters"))
        ),
        "outputs": outputs or _declared_parameters(workflow.get("outputParameters")),
    }


async def vro_execution_list_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.execution.list`` -- ``GET .../executions``, one flat row per run."""
    workflow_id = _segment("workflow", params.get("workflow_id"))
    payload = await connector._vro_get(
        target,
        _EXECUTIONS_PATH.format(workflowId=workflow_id),
        operator=operator,
        params=_page_query(params),
    )
    return _page_answer(payload.get("relations"), params)


def _run_parameters(params: Any) -> list[dict[str, Any]]:
    """``{name, type, value}`` of each run input or output; secret values replaced."""
    if not isinstance(params, list):
        return []
    return [
        {
            "name": p.get("name"),
            "type": p.get("type"),
            "value": REDACTED if _is_secret_parameter(p) else p.get("value"),
        }
        for p in params
        if isinstance(p, dict)
    ]


def _is_tls_verify_failure(exc: httpx.TransportError) -> bool:
    """True when the server answered but its certificate is not trusted.

    Same narrowing as the dispatcher: httpx raises the ``ConnectError``
    from the ``ssl.SSLCertVerificationError``; the message check covers an
    empty cause chain.
    """
    return isinstance(exc.__cause__, ssl.SSLCertVerificationError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(exc)
    )


def _is_not_reachable(exc: httpx.TransportError) -> bool:
    """True when the server did not answer; False for an error MEHO must report.

    Two transport errors are not "the server did not answer". An
    untrusted certificate means the server did answer. An
    :class:`~meho_backplane.connectors.adapters.http.SsrfBlockedError`
    means MEHO refused the destination before any request. Both are
    raised on, so the dispatcher reports the real reason.
    """
    return not (isinstance(exc, SsrfBlockedError) or _is_tls_verify_failure(exc))


def _outcome(state: Any) -> str:
    """The plain outcome of a vendor run state; ``unknown`` when the state is not known."""
    if not isinstance(state, str):
        return _OUTCOME_UNKNOWN
    return OUTCOME_BY_STATE.get(state.strip().lower(), _OUTCOME_UNKNOWN)


async def vro_execution_get_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.execution.get`` -- one run: state, outcome, dates, user, error, inputs and outputs.

    When the server does not answer (a connection or time-out error after
    the retries), returns ``outcome: not_reachable`` with the run id and
    ``checked_at``; every other field is empty. Two errors are not "not
    reachable" and are raised on: a certificate the server answered with
    but MEHO does not trust (the dispatcher returns its
    ``connector_tls_verify_failed`` answer with the fix guidance), and a
    destination MEHO refused before any request because it resolves to a
    private address (the dispatcher returns ``connector_error`` with the
    guard's message).
    """
    workflow_id = _segment("workflow", params.get("workflow_id"))
    execution_id = _segment("execution", params.get("execution_id"))
    checked_at = datetime.now(UTC).isoformat()
    try:
        payload = await connector._vro_get(
            target,
            _EXECUTION_PATH.format(workflowId=workflow_id, executionId=execution_id),
            operator=operator,
        )
    except httpx.TransportError as exc:
        if not _is_not_reachable(exc):
            raise
        payload = {"id": params.get("execution_id")}
        outcome = _OUTCOME_NOT_REACHABLE
    else:
        outcome = _outcome(payload.get("state"))
    return _redact_object(
        {
            "id": payload.get("id"),
            "name": payload.get("name"),
            "state": payload.get("state"),
            "outcome": outcome,
            "checked_at": checked_at,
            "business_state": payload.get("business-state"),
            "start_date": payload.get("start-date"),
            "end_date": payload.get("end-date"),
            "started_by": payload.get("started-by"),
            "error": payload.get("content-exception"),
            "current_item": payload.get("current-item-display-name"),
            "inputs": _run_parameters(payload.get("inputParameters")),
            "outputs": _run_parameters(payload.get("outputParameters")),
        }
    )


def _cut(text: Any, redact: TextRule) -> tuple[Any, bool]:
    """Cut *text* at :data:`LOG_TEXT_MAX`; the secret rules run on the whole text first.

    The rules need the label and the whole value. A cut could leave a
    fragment of a labelled secret too short for them to find later, so
    they run before the cut. A text that fits is left to the dispatcher.
    """
    if not isinstance(text, str) or len(text) <= LOG_TEXT_MAX:
        return text, False
    text, _ = redact(text)
    if len(text) <= LOG_TEXT_MAX:
        return text, False
    return text[:LOG_TEXT_MAX], True


def _log_row(entry: dict[str, Any], redact: TextRule) -> dict[str, Any]:
    # Older servers wrap each line in an ``entry`` object.
    wrapped = entry.get("entry")
    line: dict[str, Any] = wrapped if isinstance(wrapped, dict) else entry
    short, short_cut = _cut(line.get("short-description"), redact)
    long, long_cut = _cut(line.get("long-description"), redact)
    return {
        "time_stamp": line.get("time-stamp"),
        "severity": line.get("severity"),
        "user": line.get("userName", line.get("user")),
        "origin": line.get("origin"),
        "short_description": short,
        "long_description": long,
        "cut": short_cut or long_cut,
    }


async def vro_execution_logs_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.execution.logs`` -- the log lines of one run, size-capped.

    Asks for ``max_lines + 1`` lines (``maxResult``) and keeps at most
    ``max_lines``; the extra line only tells whether ``more_lines`` is true.
    Each text is cut at :data:`LOG_TEXT_MAX` characters, after the shared
    secret rules ran on the whole text; a cut row has ``cut: true``.
    """
    workflow_id = _segment("workflow", params.get("workflow_id"))
    execution_id = _segment("execution", params.get("execution_id"))
    max_lines = int(params.get("max_lines", _DEFAULT_LOG_LINES))
    payload = await connector._vro_get(
        target,
        _SYSLOGS_PATH.format(workflowId=workflow_id, executionId=execution_id),
        operator=operator,
        params={"maxResult": max_lines + 1},
    )
    redact = text_redactor(
        operator, connector_id=f"{connector.impl_id}-{connector.version}", op_id=_LOGS_OP_ID
    )
    logs = payload.get("logs")
    entries = [entry for entry in logs or [] if isinstance(entry, dict)]
    rows = [_log_row(entry, redact) for entry in entries[:max_lines]]
    more = len(entries) > max_lines
    return {
        "items": redact_secrets(rows),
        "lines": len(rows),
        "more_lines": more,
        "more_rows": more,
    }
