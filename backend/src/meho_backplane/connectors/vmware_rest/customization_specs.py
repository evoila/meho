# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reads of vCenter guest customization specs, and the group text they share.

A guest customization spec is a saved set of guest OS settings (host name, IP
address per NIC, DNS, Windows sysprep or Linux settings). vCenter applies it
when it clones or deploys a VM. This module holds:

* the two typed reads (their metadata is in :mod:`.typed_ops_customization_spec`):

  * ``vmware.guest.customization_spec.list`` -- ``GET /vcenter/guest/customization-specs``.
    One row per spec: name, description, OS type, last change. The optional
    ``os_type`` and ``name`` filters are applied in MEHO, so the call is the
    same on vCenter 8 and 9 (the two API files name the OS type filter
    differently).
  * ``vmware.guest.customization_spec.get`` --
    ``GET /vcenter/guest/customization-specs/{name}``. One spec in full,
    with every secret hidden (see :func:`hide_secrets`).

* :func:`read_spec`, which the governed delete
  (:mod:`.composites._customization_spec_delete`) uses for its plan and its
  read-back;
* :data:`GUEST_GROUP_KEY` / :data:`GUEST_WHEN_TO_USE`, the one text of the
  ``guest`` group. The typed reads and the composites both register it.

Both reads call the vCenter REST API on the connector's own session, so they
need no spec ingest. Each answer is a plain field copy: no field is passed on
without a check for secrets.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "GET_OP_ID",
    "GUEST_GROUP_KEY",
    "GUEST_WHEN_TO_USE",
    "LIST_OP_ID",
    "OS_TYPES",
    "SPECS_PATH",
    "SPEC_PATH",
    "customization_spec_get_impl",
    "customization_spec_list_impl",
    "hide_secrets",
    "read_spec",
    "spec_path",
    "summary_row",
]

LIST_OP_ID: Final = "vmware.guest.customization_spec.list"
GET_OP_ID: Final = "vmware.guest.customization_spec.get"

#: Spec-relative REST paths; ``mount_op_path`` puts them on ``/api`` or ``/rest``.
SPECS_PATH: Final = "/vcenter/guest/customization-specs"
SPEC_PATH: Final = "/vcenter/guest/customization-specs/{name}"

#: The values of the vendor enum ``CustomizationSpecs.OsType``.
OS_TYPES: Final[tuple[str, ...]] = ("WINDOWS", "LINUX")

GUEST_GROUP_KEY: Final = "guest"

GUEST_WHEN_TO_USE: Final = (
    "Use for guest OS customization: how a cloned or deployed VM gets its host name "
    "and IP address on first boot. Reads: list the saved customization specs "
    "(vmware.guest.customization_spec.list) and read one in full, with passwords and "
    "scripts hidden (vmware.guest.customization_spec.get). Changes: save a named "
    "spec with host name, IP per NIC, gateway and DNS, or a Windows sysprep spec "
    "(guest.customization_spec.create, dangerous); apply a saved spec to a "
    "powered-off VM, and power it on if asked (vm.customize, dangerous); delete a "
    "saved spec (guest.customization_spec.delete, destructive). Use it after a 'vm' "
    "clone or create. A spec can hold admin passwords; they never reach a reviewer."
)

#: Fields of free text that can hold a password, a key or a script with one in
#: it (Windows sysprep XML and run-once commands, the Linux script, cloud-init
#: user data). MEHO cannot tell which part is secret, so it hides the whole value.
_FREE_TEXT_FIELDS: Final[frozenset[str]] = frozenset(
    {"sysprep_xml", "gui_run_once_commands", "script_text", "userdata"}
)


def spec_path(name: str) -> str:
    """The REST path of one spec, with *name* encoded as one path segment.

    ``/``, ``?``, ``#`` and ``%`` are percent-encoded, so a name can never
    reach another path. A bare ``.`` or ``..`` is refused.
    """
    if name in {".", ".."}:
        raise ValueError(f"{name!r} is not a customization spec name")
    return SPEC_PATH.format(name=quote(name, safe=""))


def _unwrap(payload: Any) -> Any:
    """The inner ``value`` of a legacy ``/rest`` answer, else the answer itself."""
    if isinstance(payload, dict) and set(payload) == {"value"}:
        return payload["value"]
    return payload


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _os_type(data: Mapping[str, Any]) -> str | None:
    """The OS type of a summary. The wire name is ``OS_type``; the 9.0 file says ``os_type``."""
    return _text(data.get("OS_type", data.get("os_type")))


def summary_row(entry: Any) -> dict[str, Any]:
    """Project one vendor ``CustomizationSpecs.Summary`` onto a flat row."""
    data = _mapping(entry)
    return {
        "name": _text(data.get("name")),
        "description": _text(data.get("description")),
        "os_type": _os_type(data),
        "last_modified": _text(data.get("last_modified")),
    }


def _is_hidden_value(value: Any) -> bool:
    """A value worth hiding: text, or a list or object that is not empty."""
    if isinstance(value, str):
        return value != ""
    return isinstance(value, (list, dict)) and bool(value)


def hide_secrets(node: Any, path: str = "") -> tuple[Any, list[str]]:
    """Return a copy of *node* with every secret hidden, and the paths that were hidden.

    A field is hidden when its name is a secret name (``password``,
    ``domain_password``, ``product_key``, ...; the shared rule of
    :func:`~meho_backplane.broadcast.events.is_secret_param_name`) or when it is
    free text that can hold one (:data:`_FREE_TEXT_FIELDS`). Its value becomes
    :data:`~meho_backplane.redaction.secret_fields.SECRET_FIELD_MARKER`. An
    empty or ``null`` value stays, so the reader still sees that it is not set.
    """
    hidden: list[str] = []
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for key, value in node.items():
            field_path = f"{path}.{key}" if path else str(key)
            name = str(key)
            if (is_secret_param_name(name) or name in _FREE_TEXT_FIELDS) and _is_hidden_value(
                value
            ):
                out[key] = SECRET_FIELD_MARKER
                hidden.append(field_path)
                continue
            out[key], inner = hide_secrets(value, field_path)
            hidden.extend(inner)
        return out, hidden
    if isinstance(node, list):
        items: list[Any] = []
        for index, item in enumerate(node):
            clean, inner = hide_secrets(item, f"{path}[{index}]")
            items.append(clean)
            hidden.extend(inner)
        return items, hidden
    return node, hidden


async def _get(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    path: str,
) -> Any:
    mounted = await connector.mount_op_path(target, path, operator)
    return _unwrap(await connector._get_json(target, mounted, operator=operator))


async def read_spec(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    name: str,
) -> dict[str, Any] | None:
    """``GET`` one spec (the vendor ``CustomizationSpecs.Info``); ``None`` on HTTP 404.

    The answer still holds every secret. Pass it through :func:`hide_secrets`
    before it leaves MEHO. Any other HTTP error raises.
    """
    try:
        info = await _get(connector, operator, target, spec_path(name))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return dict(_mapping(info))


def _name_matches(name: str | None, pattern: str | None) -> bool:
    if pattern is None:
        return True
    return name is not None and fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


async def customization_spec_list_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.guest.customization_spec.list``. Returns ``{"specs": [...]}``."""
    answer = await _get(connector, operator, target, SPECS_PATH)
    if not isinstance(answer, list):
        raise RuntimeError(
            f"{LIST_OP_ID}: expected a list from GET {SPECS_PATH}, got {type(answer).__name__}"
        )
    os_type = _text(params.get("os_type"))
    pattern = _text(params.get("name"))
    rows = [
        row
        for row in (summary_row(entry) for entry in answer)
        if (os_type is None or row["os_type"] == os_type) and _name_matches(row["name"], pattern)
    ]
    return {"specs": rows}


def _config_kind(customization: Mapping[str, Any]) -> str | None:
    """``windows``, ``linux`` or ``cloud_init``: which part of ``configuration_spec`` is set."""
    config = _mapping(customization.get("configuration_spec"))
    for key, kind in (
        ("windows_config", "windows"),
        ("linux_config", "linux"),
        ("cloud_config", "cloud_init"),
    ):
        if config.get(key):
            return kind
    return None


async def customization_spec_get_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.guest.customization_spec.get``. Secrets are hidden."""
    name = str(params["name"])
    info = await read_spec(connector, operator, target, name)
    if info is None:
        raise ConnectorResourceNotFoundError(
            [name], f"vCenter has no guest customization spec named {name!r}"
        )
    spec = _mapping(info.get("spec"))
    customization = _mapping(spec.get("spec"))
    clean, hidden = hide_secrets(
        {
            "configuration_spec": customization.get("configuration_spec"),
            "global_dns_settings": customization.get("global_dns_settings"),
            "interfaces": customization.get("interfaces"),
        }
    )
    return {
        "name": _text(spec.get("name")) or name,
        "description": _text(spec.get("description")),
        "last_modified": _text(info.get("last_modified")),
        "fingerprint": _text(spec.get("fingerprint")),
        "config_kind": _config_kind(customization),
        "customization": clean,
        "hidden_fields": hidden,
    }
