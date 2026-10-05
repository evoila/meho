# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read helpers for the #3339 teardown deletes -- strict about partial answers.

A teardown delete may only go ahead when vCenter has **fully** answered the
question "does anything still use this object?". These helpers make an
incomplete answer visible instead of reading it as "nothing":

* a property vCenter could not read (a ``missingSet`` entry, e.g. no
  permission) is never treated as "empty";
* a paged ``RetrievePropertiesEx`` answer (a ``token``) is never treated as
  complete;
* an object that should be in the answer but is not is never skipped.

:func:`read_object` raises on an incomplete read of the object to delete (the
call fails before any write). :func:`read_many` returns a ``complete`` flag so
a usage check can refuse with a clear message.

Every moid that goes into a vim URL path is shape-checked first
(:func:`moid_problem`), so no input can point a request at another object.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.vmware_rest.composites._write import (
    _VMOMI_RETRIEVE_PROPERTIES_PATH,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "IncompleteAnswerError",
    "moid_problem",
    "moref_values",
    "read_many",
    "read_names",
    "read_object",
]

_MO_NOT_FOUND_FAULT: Final = "ManagedObjectNotFound"
#: Objects per ``RetrievePropertiesEx`` in :func:`read_many` -- small enough
#: that vCenter does not page the answer.
_CHUNK: Final = 50


class IncompleteAnswerError(RuntimeError):
    """vCenter did not fully answer a read a delete decision depends on."""


def moid_problem(value: Any, pattern: str, label: str) -> str | None:
    """Why *value* is not a ``label`` moid of the expected shape, or ``None``."""
    if isinstance(value, str) and re.fullmatch(pattern, value):
        return None
    return f"{label} must be a moid like the examples in the op description; got {value!r}"


def moref_values(raw: Any) -> list[tuple[str, str]]:
    """``[(type, moid), ...]`` from a vim ``ManagedObjectReference[]`` value."""
    refs = unwrap_vim_value(raw)
    if not isinstance(refs, list):
        return []
    out: list[tuple[str, str]] = []
    for ref in refs:
        if isinstance(ref, dict) and isinstance(ref.get("value"), str):
            out.append((str(ref.get("type") or ""), ref["value"]))
    return out


def _objects(result: Any) -> dict[str, tuple[dict[str, Any], set[str]]]:
    """``{moid: (props, missing property paths)}`` from a ``RetrieveResult``."""
    payload = _unwrap_value(result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    out: dict[str, tuple[dict[str, Any], set[str]]] = {}
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict):
            continue
        ref = obj.get("obj")
        moid = ref.get("value") if isinstance(ref, dict) else None
        if not isinstance(moid, str):
            continue
        props = {
            entry["name"]: unwrap_vim_value(entry.get("val"))
            for entry in obj.get("propSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        missing = {
            entry["path"]
            for entry in obj.get("missingSet") or []
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        out[moid] = (props, missing)
    return out


def _paged(result: Any) -> bool:
    payload = _unwrap_value(result)
    return isinstance(payload, dict) and bool(payload.get("token"))


def _json_fault_type(exc: httpx.HTTPStatusError) -> str | None:
    """The ``_typeName`` of a JSON ``VimFault`` HTTP 500 body, else ``None``."""
    if exc.response.status_code != 500:
        return None
    try:
        body = exc.response.json()
    except ValueError:
        return None
    type_name = body.get("_typeName") if isinstance(body, dict) else None
    return type_name if isinstance(type_name, str) else None


async def read_object(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    mo_type: str,
    moid: str,
    props: list[str],
) -> dict[str, Any] | None:
    """Read *props* of one managed object; ``None`` when it does not exist.

    One un-gated ``RetrievePropertiesEx``. A ``ManagedObjectNotFound`` fault
    (the SOAP-shaped body the connector promotes, or the JSON ``VimFault`` the
    pinned ``vi-json.yaml`` documents for HTTP 500) or an empty result means
    absent. An object whose requested properties are all unset reads as
    ``{}`` (present). A requested property vCenter could not read raises
    :class:`IncompleteAnswerError`; other faults propagate -- the call fails
    before any write.
    """
    try:
        result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(mo_type, [moid], props),
            promote_managed_object_not_found=True,
        )
    except ConnectorResourceNotFoundError:
        return None
    except httpx.HTTPStatusError as exc:
        if _json_fault_type(exc) == _MO_NOT_FOUND_FAULT:
            return None
        raise
    found = _objects(result).get(moid)
    if found is None:
        return None
    values, missing = found
    unread = sorted(missing & set(props))
    if unread:
        raise IncompleteAnswerError(
            f"vCenter could not read {', '.join(unread)} of {mo_type} {moid!r} (missing "
            "permission or an unreadable object); refusing to decide on a partial answer"
        )
    return values


async def read_many(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    mo_type: str,
    moids: list[str],
    props: list[str],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Read *props* of many objects: ``({moid: props}, complete)``.

    Reads in chunks of 50. ``complete`` is ``False`` when vCenter paged an
    answer, left an object out, or could not read one of *props* on an
    object -- the caller then refuses instead of reading "nothing uses it".
    Faults propagate.
    """
    by_moid: dict[str, dict[str, Any]] = {}
    complete = True
    for start in range(0, len(moids), _CHUNK):
        chunk = moids[start : start + _CHUNK]
        result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(mo_type, chunk, props),
        )
        objects = _objects(result)
        if _paged(result) or set(chunk) - set(objects):
            complete = False
        for moid, (values, missing) in objects.items():
            if missing & set(props):
                complete = False
            by_moid[moid] = values
    return by_moid, complete


async def read_names(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    mo_type: str,
    moids: list[str],
) -> dict[str, str]:
    """Best-effort ``{moid: name}`` for display only; any failure yields ``{}``."""
    if not moids:
        return {}
    try:
        result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=retrieve_properties_body(mo_type, moids, ["name"]),
        )
    except Exception:
        return {}
    return {
        moid: values["name"]
        for moid, (values, _missing) in _objects(result).items()
        if isinstance(values.get("name"), str)
    }
