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
set, or whose name says password, secret, token, private key or licence
key comes back with its value replaced by :data:`REDACTED`. Any field
with such a name, at any depth, is replaced too. The connector masks the
same fields before the flight recorder records a vendor answer.

The paths come from the vendor's Orchestrator REST API reference (9.0 and
9.1); the reconcile lane checks every path in :data:`VENDOR_CALLS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vro.session import VroTargetLike
from meho_backplane.operations._rfc6570 import has_dot_segment

if TYPE_CHECKING:
    from meho_backplane.connectors.vro.connector import VroConnector

__all__ = [
    "ABOUT_PATH",
    "DEFAULT_LIST_LIMIT",
    "LOG_TEXT_MAX",
    "REDACTED",
    "VENDOR_CALLS",
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

#: Written in place of a secret value; the key stays, so the shape is stable.
REDACTED: Final = "***REDACTED***"

#: Key names (lower case, without ``_`` and ``-``) whose value is secret.
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
    "privatekey",
    "licensekey",
    "licencekey",
)


def _is_secret_name(key: object) -> bool:
    if not isinstance(key, str):
        return False
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


def _page_query(params: dict[str, Any]) -> dict[str, Any]:
    """The vendor ``maxResult`` / ``startIndex`` query of a list read."""
    return {
        "maxResult": int(params.get("max_results", DEFAULT_LIST_LIMIT)),
        "startIndex": int(params.get("start_index", 0)),
    }


def _page_answer(envelope: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``{items, total, start_index, count}`` from a vendor list envelope."""
    page = envelope if isinstance(envelope, dict) else {}
    return {
        "items": redact_secrets(rows),
        "total": page.get("total"),
        "start_index": page.get("startIndex"),
        "count": len(rows),
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
    return _page_answer(payload, flat_rows(payload.get("link")))


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


async def vro_workflow_get_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.workflow.get`` -- the workflow and its declared inputs and outputs.

    ``GET /vco/api/workflows/{workflowId}`` gives the name, version,
    description and category; ``GET .../content`` gives the declared
    inputs and outputs. Default values are not returned: they can hold
    secrets and are not needed to call the workflow.
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
        "inputs": inputs or _declared_parameters(workflow.get("inputParameters")),
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
    relations = payload.get("relations")
    relations = relations if isinstance(relations, dict) else {}
    return _page_answer(relations, flat_rows(relations.get("link")))


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


async def vro_execution_get_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.execution.get`` -- one run: state, dates, user, error, inputs and outputs."""
    workflow_id = _segment("workflow", params.get("workflow_id"))
    execution_id = _segment("execution", params.get("execution_id"))
    payload = await connector._vro_get(
        target,
        _EXECUTION_PATH.format(workflowId=workflow_id, executionId=execution_id),
        operator=operator,
    )
    return _redact_object(
        {
            "id": payload.get("id"),
            "name": payload.get("name"),
            "state": payload.get("state"),
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


def _cut(text: Any) -> tuple[Any, bool]:
    if isinstance(text, str) and len(text) > LOG_TEXT_MAX:
        return text[:LOG_TEXT_MAX], True
    return text, False


def _log_row(entry: dict[str, Any]) -> dict[str, Any]:
    # Older servers wrap each line in an ``entry`` object.
    wrapped = entry.get("entry")
    line: dict[str, Any] = wrapped if isinstance(wrapped, dict) else entry
    short, short_cut = _cut(line.get("short-description"))
    long, long_cut = _cut(line.get("long-description"))
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

    Asks for at most ``max_lines`` lines (``maxResult``) and keeps at most
    that many even if the server sends more. Each text is cut at
    :data:`LOG_TEXT_MAX` characters; a cut row has ``cut: true``.
    """
    workflow_id = _segment("workflow", params.get("workflow_id"))
    execution_id = _segment("execution", params.get("execution_id"))
    max_lines = int(params.get("max_lines", _DEFAULT_LOG_LINES))
    payload = await connector._vro_get(
        target,
        _SYSLOGS_PATH.format(workflowId=workflow_id, executionId=execution_id),
        operator=operator,
        params={"maxResult": max_lines},
    )
    logs = payload.get("logs")
    entries = [entry for entry in logs or [] if isinstance(entry, dict)]
    rows = [_log_row(entry) for entry in entries[:max_lines]]
    return {
        "items": redact_secrets(rows),
        "lines": len(rows),
        "more_lines": len(entries) > max_lines,
    }
