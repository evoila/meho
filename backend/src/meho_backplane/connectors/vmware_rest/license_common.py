# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSphere licences: the shared vCenter reads, the key mask and the row shapes.

Three operations use this module:

* the typed reads ``vmware.license.list`` and ``vmware.license.assigned.list``
  (:mod:`.typed_ops_license`);
* the write ``vmware.composite.license.assign``
  (:mod:`.composites._license`).

A licence key is a secret. The vCenter answers these reads get hold every
key in clear, so:

* no row built here holds a full key. :func:`mask_license_key` keeps the
  last five characters and hides every other letter and digit;
* nothing here logs a key;
* the three operations carry the tag :data:`LICENSE_KEY_TAG`. Its ``key``
  token is a secret tag token of the flight recorder
  (:data:`~meho_backplane.redaction.flight_recorder.families.SECRET_FAMILY_TAGS`),
  so the flight recorder stores no request or answer body of their vCenter
  calls.

The calls go through the connector's VI-JSON path
(:meth:`~.connector.VmwareRestConnector._post_vmomi_json`), so they need
vCenter 8.0 U1 or newer.

This module imports neither :mod:`.typed_ops` nor the composites package, so
both can import it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.vim_body import (
    fault_type_name,
    retrieve_properties_body,
    unwrap_vim_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "ASSIGNMENT_ROW_SCHEMA",
    "LICENSE_GROUP_KEY",
    "LICENSE_KEY_TAG",
    "LICENSE_ROW_SCHEMA",
    "LICENSE_WHEN_TO_USE",
    "OP_QUERY_ASSIGNED_LICENSES",
    "OP_RETRIEVE_PROPERTIES",
    "OP_UPDATE_ASSIGNED_LICENSE",
    "LicenseInventory",
    "assignment_row",
    "is_evaluation",
    "license_row",
    "mask_license_key",
    "query_assigned_licenses",
    "read_license_inventory",
]

#: The operation group of the three licence operations, on both catalogs.
LICENSE_GROUP_KEY: Final = "vmware-licensing"

#: The group text. Both registrars (typed ops and composites) pass this one
#: string, so the group row reads the same whichever registrar writes it first.
LICENSE_WHEN_TO_USE: Final = (
    "Use for vSphere licences in a vCenter. Two reads: the licences in the "
    "vCenter's licence inventory (vmware.license.list: name, edition, product, "
    "used and total units, expiry; the key is masked to its last 5 characters), "
    "and which licence each asset uses (vmware.license.assigned.list: the vCenter, "
    "every host, and vSAN clusters). One change: assign a licence from that "
    "inventory to a host or to the vCenter (vmware.composite.license.assign); it "
    "waits until a second person approves. The right group for 'which licence "
    "does this host use?', 'why does a host show an expired licence?' and 'give "
    "this host the Enterprise Plus licence'. On vCenter 9, licences are usually "
    "managed in VCF Operations (the vrops licensing operations)."
)

#: Tag on all three operations. Its ``key`` token makes the flight recorder
#: drop the body of every vCenter call they make: the answers hold every
#: licence key in clear, and the assign request holds one.
LICENSE_KEY_TAG: Final = "license-key"

#: The vCenter methods the licence operations call, in the governed form
#: (``POST:/<Type>/{moId}/<Method>``) the approval gate and the reconcile
#: lanes use.
OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
OP_QUERY_ASSIGNED_LICENSES: Final = "POST:/LicenseAssignmentManager/{moId}/QueryAssignedLicenses"
OP_UPDATE_ASSIGNED_LICENSE: Final = "POST:/LicenseAssignmentManager/{moId}/UpdateAssignedLicense"

_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_LICENSE_MANAGER_MO_TYPE: Final = "LicenseManager"
#: The LicenseManager is a vCenter singleton whose moId is 'LicenseManager'.
_LICENSE_MANAGER_MOID: Final = "LicenseManager"
_PROP_LICENSES: Final = "licenses"
_PROP_ASSIGNMENT_MANAGER: Final = "licenseAssignmentManager"

#: How many characters of a key stay visible. The same rule as
#: ``vrops.license.info``.
_VISIBLE_KEY_CHARS: Final = 5
#: The key vSphere shows for its built-in evaluation licence.
_EVALUATION_KEY: Final = "00000-00000-00000-00000-00000"
_EVALUATION_EDITION: Final = "eval"


def mask_license_key(key: object) -> str | None:
    """Return *key* with only its last 5 characters visible.

    Every other letter and digit becomes ``*``. Dashes stay, so a vSphere
    key keeps its shape: ``*****-*****-*****-*****-AB12C``. A key of 10
    characters or fewer is hidden in full. A value that is not a non-empty
    string gives ``None``.
    """
    if not isinstance(key, str) or not key:
        return None
    if len(key) <= 2 * _VISIBLE_KEY_CHARS:
        return "*" * len(key)
    head, tail = key[:-_VISIBLE_KEY_CHARS], key[-_VISIBLE_KEY_CHARS:]
    return "".join(char if char == "-" else "*" for char in head) + tail


def is_evaluation(info: dict[str, Any]) -> bool:
    """``True`` when *info* (a ``LicenseManagerLicenseInfo``) is the evaluation licence."""
    return info.get("editionKey") == _EVALUATION_EDITION or info.get("licenseKey") == (
        _EVALUATION_KEY
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _property(properties: Any, key: str) -> Any:
    """The value of *key* in an un-boxed ``KeyAnyValue`` list, else ``None``."""
    if not isinstance(properties, list):
        return None
    for entry in properties:
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry.get("value")
    return None


def _labels(labels: Any) -> list[dict[str, str]]:
    """The ``KeyValue`` labels of a licence as ``[{key, value}]``."""
    if not isinstance(labels, list):
        return []
    return [
        {"key": entry["key"], "value": entry["value"]}
        for entry in labels
        if isinstance(entry, dict)
        and isinstance(entry.get("key"), str)
        and isinstance(entry.get("value"), str)
    ]


def _expiry(info: dict[str, Any], evaluation: Any = None) -> str | None:
    """The licence's ``expirationDate``, else the asset's evaluation end date."""
    expires = _property(info.get("properties"), "expirationDate")
    if expires is None and isinstance(evaluation, dict):
        expires = _property(evaluation.get("properties"), "expirationDate")
    return _text(expires)


def license_row(info: dict[str, Any]) -> dict[str, Any]:
    """One ``LicenseManagerLicenseInfo`` as a result row. The key is masked."""
    properties = info.get("properties")
    return {
        "name": _text(info.get("name")),
        "edition_key": _text(info.get("editionKey")),
        "product_name": _text(_property(properties, "ProductName")),
        "product_version": _text(_property(properties, "ProductVersion")),
        "key_masked": mask_license_key(info.get("licenseKey")),
        "is_evaluation": is_evaluation(info),
        "total": _int(info.get("total")),
        "used": _int(info.get("used")),
        "cost_unit": _text(info.get("costUnit")),
        "expires_at": _expiry(info),
        "labels": _labels(info.get("labels")),
    }


def assignment_row(assignment: dict[str, Any]) -> dict[str, Any]:
    """One ``LicenseAssignmentManagerLicenseAssignment`` as a result row.

    ``product_name`` / ``product_version`` describe the asset (for example
    ``VMware ESX Server`` for a host). The key is masked.
    """
    raw_info = assignment.get("assignedLicense")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    properties = assignment.get("properties")
    return {
        "entity_id": _text(assignment.get("entityId")),
        "entity_name": _text(assignment.get("entityDisplayName")),
        "scope": _text(assignment.get("scope")),
        "product_name": _text(_property(properties, "ProductName")),
        "product_version": _text(_property(properties, "ProductVersion")),
        "license_name": _text(info.get("name")),
        "edition_key": _text(info.get("editionKey")),
        "key_masked": mask_license_key(info.get("licenseKey")),
        "is_evaluation": is_evaluation(info),
        "expires_at": _expiry(info, _property(properties, "Evaluation")),
    }


def _nullable(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


_KEY_MASKED_FIELD: Final = _nullable(
    "string",
    "The licence key with only its last 5 characters visible, for example "
    "'*****-*****-*****-*****-AB12C'. MEHO never returns a full key.",
)
_IS_EVALUATION_FIELD: Final[dict[str, Any]] = {
    "type": "boolean",
    "description": "True for the built-in evaluation licence (the 60-day trial).",
}
_EXPIRES_AT_FIELD: Final = _nullable(
    "string",
    "End date of the licence (ISO 8601), or of the asset's evaluation period. "
    "Null when vCenter gives no end date.",
)

#: JSON schema of one :func:`license_row`.
LICENSE_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "name": _nullable("string", "Display name of the licence."),
        "edition_key": _nullable("string", "Edition key; 'eval' for the evaluation licence."),
        "product_name": _nullable(
            "string", "Product the licence is for, for example 'VMware ESX Server'."
        ),
        "product_version": _nullable("string", "Product version the licence is for."),
        "key_masked": _KEY_MASKED_FIELD,
        "is_evaluation": _IS_EVALUATION_FIELD,
        "total": _nullable("integer", "Units the licence holds, counted in cost_unit."),
        "used": _nullable("integer", "Units in use, counted in cost_unit."),
        "cost_unit": _nullable(
            "string", "What one unit counts, for example 'cpuPackage' or 'core'."
        ),
        "expires_at": _EXPIRES_AT_FIELD,
        "labels": {
            "type": "array",
            "description": "Labels set on the licence.",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Label name."},
                    "value": {"type": "string", "description": "Label value."},
                },
            },
        },
    },
    "required": ["name", "key_masked", "is_evaluation", "total", "used", "expires_at"],
}

#: JSON schema of one :func:`assignment_row`.
ASSIGNMENT_ROW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "entity_id": _nullable(
            "string",
            "Id of the asset: a host moid (for example 'host-12'), a cluster moid, or "
            "the vCenter's instance UUID. Pass it to vmware.composite.license.assign.",
        ),
        "entity_name": _nullable("string", "Display name of the asset."),
        "scope": _nullable("string", "Scope of the asset id, when vCenter sets one."),
        "product_name": _nullable(
            "string",
            "Product of the asset, for example 'VMware ESX Server' (a host) or "
            "'VMware VirtualCenter Server' (the vCenter).",
        ),
        "product_version": _nullable("string", "Product version of the asset."),
        "license_name": _nullable("string", "Display name of the licence the asset uses."),
        "edition_key": _nullable("string", "Edition key; 'eval' for the evaluation licence."),
        "key_masked": _KEY_MASKED_FIELD,
        "is_evaluation": _IS_EVALUATION_FIELD,
        "expires_at": _EXPIRES_AT_FIELD,
    },
    "required": ["entity_id", "license_name", "key_masked", "is_evaluation", "expires_at"],
}


@dataclass(frozen=True)
class LicenseInventory:
    """What the vCenter's ``LicenseManager`` holds.

    ``licenses`` are the un-boxed ``LicenseManagerLicenseInfo`` objects with
    their FULL keys. Never return, log or audit them; build rows with
    :func:`license_row`. ``assignment_manager`` is the moId of the
    ``LicenseAssignmentManager``, or ``None`` when the target has none.
    """

    licenses: tuple[dict[str, Any], ...]
    assignment_manager: str | None


def _missing_fault_types(result: Any) -> list[str]:
    """The fault type names of the ``missingSet`` of a property read."""
    names: set[str] = set()
    objects = result.get("objects") if isinstance(result, dict) else None
    for obj in objects if isinstance(objects, list) else []:
        for miss in (obj.get("missingSet") or []) if isinstance(obj, dict) else []:
            name = fault_type_name(miss.get("fault")) if isinstance(miss, dict) else None
            if name is not None:
                names.add(name)
    return sorted(names)


def _property_values(result: Any) -> dict[str, Any]:
    """Flatten the ``propSet`` of a property read to ``{name: un-boxed value}``."""
    values: dict[str, Any] = {}
    objects = result.get("objects") if isinstance(result, dict) else None
    for obj in objects if isinstance(objects, list) else []:
        for prop in (obj.get("propSet") or []) if isinstance(obj, dict) else []:
            if isinstance(prop, dict) and isinstance(prop.get("name"), str):
                values[prop["name"]] = unwrap_vim_value(prop.get("val"))
    return values


async def read_license_inventory(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
) -> LicenseInventory:
    """Read ``LicenseManager.licenses`` and ``.licenseAssignmentManager`` in one call.

    Raises :exc:`RuntimeError` when vCenter returns no ``licenses`` property,
    naming the fault type of the missing property (for example
    ``NoPermission``) but never fault text.
    """
    result = await connector._post_vmomi_json(
        target,
        _RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(
            _LICENSE_MANAGER_MO_TYPE,
            [_LICENSE_MANAGER_MOID],
            [_PROP_LICENSES, _PROP_ASSIGNMENT_MANAGER],
        ),
    )
    values = _property_values(result)
    if _PROP_LICENSES not in values:
        faults = ", ".join(_missing_fault_types(result)) or "no fault given"
        raise RuntimeError(
            f"vCenter returned no LicenseManager.licenses property ({faults}); check "
            "that the MEHO account may read licences, and that the target is a vCenter"
        )
    raw = values[_PROP_LICENSES]
    licenses = (
        tuple(item for item in raw if isinstance(item, dict)) if isinstance(raw, list) else ()
    )
    manager = values.get(_PROP_ASSIGNMENT_MANAGER)
    manager_moid = manager.get("value") if isinstance(manager, dict) else None
    return LicenseInventory(
        licenses=licenses,
        assignment_manager=manager_moid if isinstance(manager_moid, str) and manager_moid else None,
    )


async def query_assigned_licenses(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    assignment_manager: str,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Call ``LicenseAssignmentManager.QueryAssignedLicenses``; return the assignments.

    With *entity_id*, vCenter answers for that one asset only. The
    assignments are un-boxed and keep their FULL keys: build rows with
    :func:`assignment_row`.
    """
    result = await connector._post_vmomi_json(
        target,
        f"/LicenseAssignmentManager/{assignment_manager}/QueryAssignedLicenses",
        operator=operator,
        json={"entityId": entity_id} if entity_id else {},
    )
    payload = unwrap_vim_value(result)
    if payload is None or payload == {}:
        return []
    if not isinstance(payload, list):
        raise RuntimeError(
            "QueryAssignedLicenses returned "
            f"{type(payload).__name__}, not a list of licence assignments"
        )
    return [item for item in payload if isinstance(item, dict)]
