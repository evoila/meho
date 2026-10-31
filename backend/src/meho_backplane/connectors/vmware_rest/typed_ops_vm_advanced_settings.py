# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vmware.vm.advanced_settings.show`` -- the advanced settings of one VM.

A VM's advanced settings are its ``extraConfig`` key and value pairs (for
example ``disk.EnableUUID`` or ``isolation.tools.copy.disable``). The vCenter
REST API has no path for them, so this typed read uses the VIM API: one
``RetrievePropertiesEx`` of ``name`` and ``config.extraConfig``. It needs no
catalog ingest and works on vCenter 8 and 9.

The answer is a flat list of ``{key, value, masked}`` rows, sorted by key, so
a long list goes behind a result handle like any other list.

Some keys can hold a secret: a cloud-init user-data blob, an OVF environment
with passwords, an Ignition or Talos guest config, or any key whose name says
password, secret, token, key material or licence. MEHO never returns those
values: the row keeps its key, ``value`` is ``null`` and ``masked`` is true.
:func:`is_secret_key` is shared with the change
``vmware.composite.vm.advanced_settings.set``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog

from meho_backplane.connectors.vmware_rest._vm_settings_schemas import (
    ADVANCED_SETTING_ROW_SCHEMA,
    VM_ADVANCED_SETTINGS_PARAMETER_SCHEMA,
    VM_ADVANCED_SETTINGS_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.typed_ops import VmwareTypedOp
from meho_backplane.connectors.vmware_rest.typed_ops_vm_hardware import (
    VM_HARDWARE_GROUP_KEY,
    resolve_vm,
)
from meho_backplane.connectors.vmware_rest.vim_vm_props import (
    RETRIEVE_PROPERTIES_CALL,
    read_vm_vim_props,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "ADVANCED_SETTINGS_REST_CALLS",
    "ADVANCED_SETTINGS_VIM_CALLS",
    "ADVANCED_SETTING_ROW_SCHEMA",
    "VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP",
    "is_secret_key",
    "read_extra_config",
    "setting_row",
    "vm_advanced_settings_show_impl",
]

_log = structlog.get_logger(__name__)

OP_ID: Final = "vmware.vm.advanced_settings.show"

#: The REST call of the read (the VM name lookup only).
ADVANCED_SETTINGS_REST_CALLS: Final[tuple[str, ...]] = ("GET:/vcenter/vm",)
#: The VIM call of the read, as a ``METHOD:/path`` key of ``vi-json.yaml``.
ADVANCED_SETTINGS_VIM_CALLS: Final[tuple[str, ...]] = (RETRIEVE_PROPERTIES_CALL,)

_PROP_NAME: Final = "name"
_PROP_EXTRA_CONFIG: Final = "config.extraConfig"

#: Keys that carry whole guest data blobs, which often hold passwords.
_SECRET_KEYS: Final = frozenset(
    {
        "guestinfo.userdata",
        "guestinfo.vendordata",
        "guestinfo.ovfenv",
        "guestinfo.ignition.config.data",
        "guestinfo.talos.config",
    }
)
#: Key prefixes of guest config families whose values may hold a secret.
_SECRET_PREFIXES: Final[tuple[str, ...]] = ("guestinfo.ignition.", "guestinfo.talos.")
#: Words that mark a key as secret, in the lower-case key with ``_`` and ``-`` removed.
_SECRET_WORDS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "privatekey",
    "apikey",
    "credential",
    "licensekey",
    "licencekey",
)


def is_secret_key(key: str) -> bool:
    """Whether the value of the advanced setting *key* may hold a secret."""
    lowered = key.lower()
    if lowered in _SECRET_KEYS or lowered.startswith(_SECRET_PREFIXES):
        return True
    squashed = lowered.replace("_", "").replace("-", "")
    return any(word in squashed for word in _SECRET_WORDS)


def setting_row(key: str, value: Any) -> dict[str, Any]:
    """One ``{key, value, masked}`` row; a secret value is never returned."""
    if is_secret_key(key):
        return {"key": key, "value": None, "masked": True}
    text = value if isinstance(value, str) else (None if value is None else str(value))
    return {"key": key, "value": text, "masked": False}


async def read_extra_config(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    vm: str,
    *,
    op_id: str = OP_ID,
) -> tuple[str | None, dict[str, Any]]:
    """Return ``(VM name, {key: raw value})`` of *vm*'s ``extraConfig``.

    The raw values are for comparison only; callers return them through
    :func:`setting_row`. Raises ``not_found`` for an unknown VM, and
    :exc:`RuntimeError` when vCenter could not read ``config.extraConfig``
    (for example a missing permission), so a failed read never looks like
    "no settings".
    """
    props, missing = await read_vm_vim_props(
        connector, operator, target, vm, (_PROP_NAME, _PROP_EXTRA_CONFIG), op_id=op_id
    )
    if _PROP_EXTRA_CONFIG in missing:
        raise RuntimeError(
            f"{op_id}: vCenter could not read the advanced settings of VM {vm!r} "
            "(missing permission or an unreadable VM)"
        )
    settings: dict[str, Any] = {}
    raw = props.get(_PROP_EXTRA_CONFIG)
    for option in raw if isinstance(raw, list) else []:
        if isinstance(option, dict) and isinstance(option.get("key"), str):
            settings[option["key"]] = option.get("value")
    name = props.get(_PROP_NAME)
    return (name if isinstance(name, str) else None), settings


async def vm_advanced_settings_show_impl(
    connector: VmwareRestConnector,
    operator: Operator,
    target: VsphereTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Implementation of ``vmware.vm.advanced_settings.show``. See the module docstring."""
    vm = await resolve_vm(connector, operator, target, params)
    vm_name, settings = await read_extra_config(connector, operator, target, vm)
    prefix = params.get("key_prefix")
    keys = sorted(key for key in settings if not isinstance(prefix, str) or key.startswith(prefix))
    rows = [setting_row(key, settings[key]) for key in keys]
    masked = sum(1 for row in rows if row["masked"])
    _log.info("vmware_vm_advanced_settings_read", target=target.name, vm=vm, rows=len(rows))
    return {
        "vm": vm,
        "vm_name": vm_name,
        "key_prefix": prefix if isinstance(prefix, str) else None,
        "setting_count": len(rows),
        "masked_count": masked,
        "settings": rows,
    }


VMWARE_VM_ADVANCED_SETTINGS_SHOW_OP = VmwareTypedOp(
    op_id=OP_ID,
    handler_attr="vm_advanced_settings_show",
    summary="One VM's advanced settings (extraConfig) as key and value rows.",
    description=(
        "Returns the advanced settings (extraConfig) of one VM, by id (vm) or name, as a "
        "flat list of key and value rows sorted by key. Filter with key_prefix. Values "
        "that may hold a secret are masked. Read-only."
    ),
    parameter_schema=VM_ADVANCED_SETTINGS_PARAMETER_SCHEMA,
    response_schema=VM_ADVANCED_SETTINGS_RESPONSE_SCHEMA,
    group_key=VM_HARDWARE_GROUP_KEY,
    tags=("read-only", "vmware", "vcenter", "vm", "advanced-settings"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read a VM's advanced settings (extraConfig), for example "
            "disk.EnableUUID, isolation.tools.* hardening keys or guestinfo.* keys. Use "
            "key_prefix to read one family of keys. Call it before changing settings."
        ),
        "parameter_hints": {
            "vm": "VM id such as 'vm-42'; give exactly one of vm / name.",
            "name": "VM name; an unknown name returns status not_found.",
            "key_prefix": "Only keys that start with this text, such as 'guestinfo.'.",
        },
        "output_shape": (
            "{vm, vm_name, key_prefix, setting_count, masked_count, settings: [{key, value, "
            "masked}]}. masked true means the value may hold a secret; MEHO does not return "
            "it. A long list comes back behind a result handle; read it with result_query."
        ),
        "next_step": (
            "To set or remove settings, call vmware.composite.vm.advanced_settings.set with "
            "the VM id."
        ),
    },
)
