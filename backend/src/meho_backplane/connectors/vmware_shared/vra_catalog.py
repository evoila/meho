# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Catalog items and requests of Aria Automation 8 and VCF Automation 9.

Aria Automation 8.18 and the tenant plane of a VCF Automation 9 "VM Apps"
org serve the same catalog and request APIs. So the six operations are
written once here and registered on both connectors:

* ``vcfa-vra8-8.0`` with ids ``vcfa.vra8.*`` (:mod:`..vra8.catalog_ops`),
* ``vcfa-rest-9.0`` with ids ``vcfa.tenant.*``
  (:mod:`..vcf_automation.catalog_ops`).

Each connector passes a :class:`VraCalls`: its own GET and POST with its
own login, plus the op id prefix for the texts that name the next op.

The paths come from the vendor's ``vra-catalog-deployment.json`` (the
Swagger 2.0 file of ``vmware/vra-sdk-go``). Both reconcile lanes check
them against that file on the spec shelf.

Secrets:

* A request input that is a secret is never a value. The caller gives
  ``secret_inputs``: top-level input name -> ``"<vault-path>#<field>"``.
  The value is read from Vault under the caller's identity only when the
  approved call runs. It goes only into the request body. A vendor error
  text that repeats it gets a marker in its place, also when the text is
  JSON that escapes the value. A connector that records a trace gets the
  values too, so it can replace them in what it records.
* An input in ``inputs`` is refused when its name is a secret name or when
  the form marks its field as a secret (``encrypted``, ``format: password``
  or ``writeOnly``), at any depth: also inside an object or a list.
* Results: the default of a secret form field (also inside an object or a
  list default), and secret-named values in request inputs, outputs, event
  details and log lines, are replaced with a marker.

An upstream 4xx/5xx raises :exc:`httpx.HTTPStatusError`; the dispatcher
turns it into ``connector_error`` with the vendor's message.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors._shared.vault_creds import (
    load_vault_secret_data,
    strip_credential_value,
)
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, scrub_secret_fields

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "CATALOG_ITEMS_PATH",
    "CATALOG_ITEM_PATH",
    "CATALOG_ITEM_REQUEST_PATH",
    "CATALOG_ITEM_VERSION_PATH",
    "DEPLOYMENTS_PATH",
    "DEPLOYMENT_PATH",
    "REQUEST_EVENTS_PATH",
    "REQUEST_EVENT_LOGS_PATH",
    "REQUEST_PATH",
    "VraCalls",
    "catalog_item_get",
    "catalog_item_list",
    "catalog_item_request",
    "parse_secret_ref",
    "replace_secret_values",
    "request_events",
    "request_get",
    "request_logs",
]

# Vendor paths (vra-catalog-deployment.json). The method of each is in the
# reconcile lanes.
CATALOG_ITEMS_PATH: Final[str] = "/catalog/api/items"
CATALOG_ITEM_PATH: Final[str] = "/catalog/api/items/{id}"
CATALOG_ITEM_VERSION_PATH: Final[str] = "/catalog/api/items/{id}/versions/{versionId}"
CATALOG_ITEM_REQUEST_PATH: Final[str] = "/catalog/api/items/{id}/request"
#: Used by the request op: the "same name in this project" check (GET with
#: ``name`` and ``projects``) and the read of the new request (GET with
#: ``expand=lastRequest``).
DEPLOYMENTS_PATH: Final[str] = "/deployment/api/deployments"
DEPLOYMENT_PATH: Final[str] = "/deployment/api/deployments/{deploymentId}"
REQUEST_PATH: Final[str] = "/deployment/api/requests/{requestId}"
REQUEST_EVENTS_PATH: Final[str] = "/deployment/api/requests/{requestId}/events"
REQUEST_EVENT_LOGS_PATH: Final[str] = "/deployment/api/requests/{requestId}/events/{eventId}/logs"

GetJson = Callable[[str, Mapping[str, Any] | None], Awaitable[dict[str, Any]]]
#: ``post(path, body, secrets)``: *secrets* are the secret values inside
#: *body*. A connector that records a trace replaces them in what it records
#: (:func:`replace_secret_values`); they never go anywhere else.
PostJson = Callable[[str, Mapping[str, Any], Sequence[str]], Awaitable[Any]]


@dataclass(frozen=True)
class VraCalls:
    """What a connector hands the shared handlers.

    ``get`` and ``post`` send one request with the connector's login and
    return the parsed JSON (``post`` may return a list; it also gets the
    secret values of the body, see :data:`PostJson`). ``prefix`` is the
    op id prefix (``vcfa.vra8`` or ``vcfa.tenant``). ``operator`` and
    ``target`` are used only for the Vault read of ``secret_inputs``.
    """

    get: GetJson
    post: PostJson
    prefix: str
    operator: Operator
    target: Any


def _seg(value: Any) -> str:
    """Percent-encode one path segment (no ``/`` left), like the other VCFA ops."""
    return quote(str(value), safe="")


def _ref_id(value: Any) -> Any:
    return value.get("id") if isinstance(value, Mapping) else None


def _ref_name(value: Any) -> Any:
    return value.get("name") if isinstance(value, Mapping) else None


def _content(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = page.get("content")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Catalog items
# ---------------------------------------------------------------------------


def _item_row(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "description": item.get("description"),
        "typeId": _ref_id(item.get("type")),
        "typeName": _ref_name(item.get("type")),
        "sourceId": item.get("sourceId"),
        "sourceName": item.get("sourceName"),
        "projectIds": item.get("projectIds") or [],
        "formId": item.get("formId"),
        "bulkRequestLimit": item.get("bulkRequestLimit"),
        "createdAt": item.get("createdAt"),
        "lastUpdatedAt": item.get("lastUpdatedAt"),
    }


async def catalog_item_list(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.catalog.item.list`` — ``GET /catalog/api/items`` as flat rows."""
    query: dict[str, Any] = {k: params[k] for k in ("search", "$top", "$skip") if k in params}
    if params.get("project_id"):
        query["projects"] = params["project_id"]
    page = await calls.get(CATALOG_ITEMS_PATH, query or None)
    return {
        "items": [_item_row(item) for item in _content(page)],
        "totalElements": page.get("totalElements"),
    }


def _is_secret_field(name: str, definition: Mapping[str, Any]) -> bool:
    return (
        definition.get("encrypted") is True
        or definition.get("format") == "password"
        or definition.get("writeOnly") is True
        or is_secret_param_name(name)
    )


def _clean_value(name: str, value: Any, definition: Any) -> Any:
    """*value* of the form field *name* with each secret part replaced.

    A secret field (by *definition* or by name) gets the marker. An object
    is walked against the field's ``properties``, a list against its
    ``items``, so a secret inside an object or a list default is found too.
    """
    field = definition if isinstance(definition, Mapping) else {}
    if _is_secret_field(name, field):
        return value if value in (None, "") else SECRET_FIELD_MARKER
    if isinstance(value, Mapping):
        properties = field.get("properties")
        nested = properties if isinstance(properties, Mapping) else {}
        return {key: _clean_value(str(key), entry, nested.get(key)) for key, entry in value.items()}
    if isinstance(value, list):
        return [_clean_value(name, entry, field.get("items")) for entry in value]
    return value


def _clean_schema(schema: Any) -> Any:
    """Copy *schema* with the ``default`` of each secret field replaced.

    The field definitions stay, so the form still shows the field.
    """
    if isinstance(schema, list):
        return [_clean_schema(entry) for entry in schema]
    if not isinstance(schema, Mapping):
        return schema
    cleaned = {key: _clean_schema(value) for key, value in schema.items()}
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        for name, definition in properties.items():
            if isinstance(definition, dict) and "default" in definition:
                definition["default"] = _clean_value(str(name), definition["default"], definition)
    if "default" in cleaned:
        cleaned["default"] = _clean_value("", cleaned["default"], cleaned)
    return cleaned


def _input_rows(schema: Any) -> list[dict[str, Any]]:
    """One flat row per top-level request input of a cleaned form schema."""
    if not isinstance(schema, Mapping) or not isinstance(schema.get("properties"), Mapping):
        return []
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    rows: list[dict[str, Any]] = []
    for name, definition in schema["properties"].items():
        field = definition if isinstance(definition, Mapping) else {}
        rows.append(
            {
                "name": name,
                "type": field.get("type"),
                "title": field.get("title"),
                "description": field.get("description"),
                "default": field.get("default"),
                "required": name in required_names,
                "enum": field.get("enum"),
                "secret": _is_secret_field(str(name), field),
            }
        )
    return rows


async def catalog_item_get(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.catalog.item.get`` — the item and its request form.

    ``GET /catalog/api/items/{id}``; with ``version_id`` also
    ``GET /catalog/api/items/{id}/versions/{versionId}``, whose form wins.
    """
    item_id = _seg(params["id"])
    item = await calls.get(CATALOG_ITEM_PATH.format(id=item_id), None)
    version: dict[str, Any] | None = None
    schema = item.get("schema")
    if params.get("version_id"):
        path = CATALOG_ITEM_VERSION_PATH.format(id=item_id, versionId=_seg(params["version_id"]))
        found = await calls.get(path, None)
        version = {key: found.get(key) for key in ("id", "description", "createdAt", "formId")}
        schema = found.get("schema")
    cleaned = _clean_schema(schema) if schema is not None else None
    return {
        "item": _item_row(item),
        "version": version,
        "inputs": _input_rows(cleaned),
        "schema": cleaned,
    }


# ---------------------------------------------------------------------------
# Request a catalog item
# ---------------------------------------------------------------------------


def parse_secret_ref(raw: Any) -> tuple[str, str]:
    """Check one ``"<vault-path>#<field>"`` reference; return ``(path, field)``.

    Raises :class:`ValueError` with a text that names no value.
    """
    if not isinstance(raw, str) or raw.count("#") != 1:
        raise ValueError("a secret input must be one '<vault-path>#<field>' reference")
    if any(ch < " " or ch == "\x7f" for ch in raw):
        raise ValueError("a secret input reference contains a control character")
    path, _, field = raw.partition("#")
    if not path.strip() or not field.strip():
        raise ValueError("a secret input reference needs a vault path and a field")
    return path.strip(), field.strip()


@dataclass(slots=True)
class _VaultRef:
    """Points the Vault read at one reference; carries the target name for the log."""

    name: str
    host: str
    secret_ref: str | None


async def _read_secret(calls: VraCalls, ref: tuple[str, str]) -> str | None:
    path, field = ref
    host = getattr(calls.target, "host", None)
    data = await load_vault_secret_data(
        _VaultRef(
            name=str(getattr(calls.target, "name", None) or "unknown"),
            host=host if isinstance(host, str) and host else "unknown",
            secret_ref=path,
        ),
        calls.operator,
    )
    raw = data.get(field) if isinstance(data, dict) else None
    if not isinstance(raw, str):
        return None
    return strip_credential_value(raw) or None


def _invalid(guidance: str) -> dict[str, Any]:
    return {"status": "invalid_request", "deployment": None, "request": None, "guidance": guidance}


def _secret_input_paths(value: Any, definition: Any, name: str, path: str) -> list[str]:
    """The paths inside the input *value* that hold a secret, by name or by the form.

    Walks an object against the field's ``properties`` and a list against
    its ``items``, like :func:`_clean_value`. Without a *definition* only
    the names count. An empty value is not a secret.
    """
    field = definition if isinstance(definition, Mapping) else {}
    if _is_secret_field(name, field):
        return [] if value in (None, "") else [path]
    if isinstance(value, Mapping):
        properties = field.get("properties")
        nested = properties if isinstance(properties, Mapping) else {}
        return [
            found
            for key, entry in value.items()
            for found in _secret_input_paths(
                entry, nested.get(key), str(key), f"{path}.{key}" if path else str(key)
            )
        ]
    if isinstance(value, list):
        return [
            found
            for index, entry in enumerate(value)
            for found in _secret_input_paths(entry, field.get("items"), name, f"{path}[{index}]")
        ]
    return []


def _secrets_guidance(paths: list[str]) -> str:
    return (
        f"inputs {paths} are secrets; store each value in Vault and give its place in "
        "secret_inputs as '<vault-path>#<field>', never the value"
    )


def _check_inputs(params: Mapping[str, Any]) -> tuple[dict[str, tuple[str, str]], str | None]:
    """Parse ``secret_inputs`` and refuse a secret-named value in ``inputs``, at any depth."""
    inputs = params.get("inputs") or {}
    secret_inputs = params.get("secret_inputs") or {}
    named = _secret_input_paths(inputs, None, "", "")
    if named:
        return {}, _secrets_guidance(named)
    both = sorted(set(inputs) & set(secret_inputs))
    if both:
        return {}, f"inputs {both} are in both inputs and secret_inputs; give each one once"
    refs: dict[str, tuple[str, str]] = {}
    for name, raw in secret_inputs.items():
        try:
            refs[str(name)] = parse_secret_ref(raw)
        except ValueError as exc:
            return {}, f"secret_inputs[{name!r}]: {exc}"
    return refs, None


async def _existing_deployment(
    calls: VraCalls, name: str, project_id: str
) -> dict[str, Any] | None:
    page = await calls.get(DEPLOYMENTS_PATH, {"name": name, "projects": project_id})
    for row in _content(page):
        same_name = str(row.get("name", "")).casefold() == name.casefold()
        if same_name and row.get("projectId") in (None, project_id):
            return row
    return None


def replace_secret_values(node: Any, secrets: Sequence[str]) -> Any:
    """*node* (JSON data or text) with each of *secrets* replaced by the marker.

    Text is searched for the plain value and for its JSON-escaped form, so
    a value with a quote or a backslash is found inside a JSON text too.
    Data is walked; each string in it is handled as text.
    """
    if isinstance(node, str):
        for value in secrets:
            if value:
                node = node.replace(value, SECRET_FIELD_MARKER)
                node = node.replace(json.dumps(value)[1:-1], SECRET_FIELD_MARKER)
        return node
    if isinstance(node, Mapping):
        return {
            replace_secret_values(key, secrets): replace_secret_values(value, secrets)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [replace_secret_values(entry, secrets) for entry in node]
    return node


def _without_secrets(exc: httpx.HTTPStatusError, values: list[str]) -> httpx.HTTPStatusError:
    """The same error with each secret value in the vendor text replaced.

    A JSON body is decoded first, so an escaped value is found inside its
    strings; the body is then written again in the same shape. Any other
    text is searched as it is.
    """
    text = exc.response.text
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if decoded is not None:
        text = json.dumps(replace_secret_values(decoded, values), ensure_ascii=False)
    text = replace_secret_values(text, values)
    headers = {k: v for k, v in exc.response.headers.items() if k.lower() == "content-type"}
    response = httpx.Response(
        exc.response.status_code, headers=headers, content=text.encode(), request=exc.request
    )
    return httpx.HTTPStatusError(str(exc), request=exc.request, response=response)


async def _form(calls: VraCalls, params: Mapping[str, Any]) -> Any:
    """The request form of the item, or of the requested ``version`` when given."""
    item_id = _seg(params["id"])
    if params.get("version"):
        path = CATALOG_ITEM_VERSION_PATH.format(id=item_id, versionId=_seg(params["version"]))
        return (await calls.get(path, None)).get("schema")
    return (await calls.get(CATALOG_ITEM_PATH.format(id=item_id), None)).get("schema")


async def _new_request(calls: VraCalls, deployment_id: str) -> dict[str, Any] | None:
    """The request that builds the new deployment, or ``None`` if it cannot be read yet."""
    path = DEPLOYMENT_PATH.format(deploymentId=_seg(deployment_id))
    try:
        deployment = await calls.get(path, {"expand": "lastRequest"})
    except httpx.HTTPStatusError:
        return None
    last = deployment.get("lastRequest")
    if not isinstance(last, Mapping):
        return None
    return {key: last.get(key) for key in ("id", "name", "status")}


async def catalog_item_request(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.catalog.item.request`` — ``POST /catalog/api/items/{id}/request``.

    Reads the item's form first and refuses a plain value for a field the
    form marks as a secret. Safe to run twice: when the project already has
    a deployment with this name, nothing is requested and the answer is
    ``unchanged``.
    """
    refs, problem = _check_inputs(params)
    if problem is not None:
        return _invalid(problem)
    flagged = _secret_input_paths(params.get("inputs") or {}, await _form(calls, params), "", "")
    if flagged:
        return _invalid(_secrets_guidance(flagged))
    name, project_id = str(params["deployment_name"]), str(params["project_id"])
    existing = await _existing_deployment(calls, name, project_id)
    if existing is not None:
        return {
            "status": "unchanged",
            "deployment": {
                "id": existing.get("id"),
                "name": existing.get("name"),
                "projectId": project_id,
            },
            "request": None,
            "guidance": (
                f"a deployment named {name!r} already exists in this project; nothing was requested"
            ),
        }
    inputs = dict(params.get("inputs") or {})
    secrets: list[str] = []
    for input_name, ref in refs.items():
        value = await _read_secret(calls, ref)
        if value is None:
            return _invalid(
                f"secret_inputs[{input_name!r}]: the secret at {ref[0]!r} has no usable "
                f"text in field {ref[1]!r}"
            )
        inputs[input_name] = value
        secrets.append(value)
    body: dict[str, Any] = {"projectId": project_id, "deploymentName": name, "inputs": inputs}
    for param, wire in (("version", "version"), ("reason", "reason")):
        if params.get(param):
            body[wire] = params[param]
    path = CATALOG_ITEM_REQUEST_PATH.format(id=_seg(params["id"]))
    try:
        answer = await calls.post(path, body, secrets)
    except httpx.HTTPStatusError as exc:
        if not secrets:
            raise
        raise _without_secrets(exc, secrets) from None
    rows = answer if isinstance(answer, list) else [answer]
    first = next((row for row in rows if isinstance(row, Mapping)), {})
    deployment_id = first.get("deploymentId")
    request = await _new_request(calls, str(deployment_id)) if deployment_id else None
    wait = (
        f"read {calls.prefix}.request.get with request.id until status is "
        "SUCCESSFUL, FAILED or ABORTED"
    )
    return {
        "status": "submitted",
        "deployment": {
            "id": deployment_id,
            "name": first.get("deploymentName") or name,
            "projectId": project_id,
        },
        "request": request,
        "guidance": wait
        if request is not None
        else (
            "the request was sent, but its id could not be read yet; read the deployment "
            "again soon and use its lastRequest id with "
            f"{calls.prefix}.request.get"
        ),
    }


# ---------------------------------------------------------------------------
# Requests: state, events and logs
# ---------------------------------------------------------------------------


async def request_get(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.request.get`` — ``GET /deployment/api/requests/{requestId}``."""
    request = await calls.get(REQUEST_PATH.format(requestId=_seg(params["request_id"])), None)
    cleaned: dict[str, Any] = scrub_secret_fields(request)
    return cleaned


def _event_row(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "timestamp": event.get("timestamp"),
        "details": event.get("details"),
        "resourceName": event.get("resourceName"),
        "resourceType": event.get("resourceType"),
        "hasLogs": event.get("hasLogs"),
        "userEvent": event.get("userEvent"),
    }


async def request_events(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.request.events`` — the request's events, oldest first.

    Reads ``GET /deployment/api/requests/{requestId}`` for the request state
    and ``GET .../events`` for the events. The last event is named apart:
    on a failed request it is the step that failed.
    """
    request_id = _seg(params["request_id"])
    request = await calls.get(REQUEST_PATH.format(requestId=request_id), None)
    page = await calls.get(REQUEST_EVENTS_PATH.format(requestId=request_id), None)
    events = sorted(
        (_event_row(event) for event in _content(page)),
        key=lambda row: str(row.get("timestamp") or ""),
    )
    last = events[-1] if events else {}
    cleaned: dict[str, Any] = scrub_secret_fields(
        {
            "request_status": request.get("status"),
            "request_details": request.get("details"),
            "last_event_id": last.get("id"),
            "last_event_name": last.get("name"),
            "events": events,
        }
    )
    return cleaned


async def request_logs(calls: VraCalls, params: Mapping[str, Any]) -> dict[str, Any]:
    """``<prefix>.request.logs`` — ``GET .../requests/{requestId}/events/{eventId}/logs``."""
    path = REQUEST_EVENT_LOGS_PATH.format(
        requestId=_seg(params["request_id"]), eventId=_seg(params["event_id"])
    )
    query = {"sinceRow": params["since_row"]} if params.get("since_row") is not None else None
    page = await calls.get(path, query)
    lines = [
        {
            "rownum": row.get("rownum"),
            "timestamp": row.get("timestamp"),
            "message": row.get("message"),
        }
        for row in _content(page)
    ]
    eof = any(row.get("eof") is True for row in _content(page))
    rownums = [row["rownum"] for row in lines if isinstance(row["rownum"], int)]
    cleaned: dict[str, Any] = scrub_secret_fields(
        {
            "lines": lines,
            "eof": eof,
            "next_since_row": max(rownums) + 1 if rownums and not eof else None,
        }
    )
    return cleaned
