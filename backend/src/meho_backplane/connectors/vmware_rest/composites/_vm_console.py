# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Two VM console changes: take a screenshot, and press special keys.

* ``vmware.composite.vm.screenshot`` (``caution``) -- one governed
  ``VirtualMachine.CreateScreenshot_Task``, polled to the end. The task
  result is the datastore path of the PNG file vCenter wrote into the VM's
  folder. MEHO then reads that file over vCenter's datastore file access
  (``GET /folder/{path}?dcPath={datacenter}&dsName={datastore}``) on the
  connector session, streamed with a byte cap, and returns it as base64 rows
  (set-shaped, so a long image becomes a result handle).
* ``vmware.composite.vm.keys.send`` (``dangerous``) -- one governed
  ``VirtualMachine.PutUsbScanCodes`` with the USB HID codes of keys from a
  fixed list (:data:`._vm_console_pci_schemas.CONSOLE_KEYS`). No letters or
  digits, so the call cannot type a command. A key that is not on the list is
  refused before any vendor call.

Both need the VM powered on. A vCenter fault reaches the answer with its
fault type and text.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors.vmware_rest.composites._datastore_browse import (
    DATACENTER_MO_TYPE,
    resolve_datacenter,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moid_problem,
    read_object,
)
from meho_backplane.connectors.vmware_rest.composites._vm_console_pci_schemas import (
    CONSOLE_KEYS,
    SCREENSHOT_DEFAULT_MAX_BYTES,
    SCREENSHOT_HARD_CAP_BYTES,
    VM_KEYS_SEND_LLM_INSTRUCTIONS,
    VM_KEYS_SEND_PARAMETER_SCHEMA,
    VM_KEYS_SEND_RESPONSE_SCHEMA,
    VM_SCREENSHOT_LLM_INSTRUCTIONS,
    VM_SCREENSHOT_PARAMETER_SCHEMA,
    VM_SCREENSHOT_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _VIRTUAL_MACHINE_MO_TYPE,
    _task_timeout_issue,
    _unwrap_value,
    _vsphere_fault_detail,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import VM_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY, unwrap_vim_value
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "COMPOSITES",
    "VM_CONSOLE_GOVERNED_SUBOPS",
    "vm_keys_send_composite",
    "vm_screenshot_composite",
]

OP_ID_VM_SCREENSHOT: Final = "vmware.composite.vm.screenshot"
OP_ID_VM_KEYS_SEND: Final = "vmware.composite.vm.keys.send"

_OP_CREATE_SCREENSHOT_TASK: Final = "POST:/VirtualMachine/{moId}/CreateScreenshot_Task"
_OP_PUT_USB_SCAN_CODES: Final = "POST:/VirtualMachine/{moId}/PutUsbScanCodes"

#: Every VI-JSON method of each change (the reconcile tests check them against
#: the pinned 8.0 and 9.0 VI-JSON files). The screenshot's file read is
#: vCenter's datastore file access (``GET /folder/...``), which is in neither
#: API file.
_VIM_SUB_OPS_VM_SCREENSHOT: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CREATE_SCREENSHOT_TASK,
)
_VIM_SUB_OPS_VM_KEYS_SEND: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_PUT_USB_SCAN_CODES,
)

VM_CONSOLE_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    OP_ID_VM_SCREENSHOT: _VIM_SUB_OPS_VM_SCREENSHOT,
    OP_ID_VM_KEYS_SEND: _VIM_SUB_OPS_VM_KEYS_SEND,
}

_PROP_POWER: Final = "runtime.powerState"
_SCAN_CODE_SPEC_TYPE: Final = "UsbScanCodeSpec"
_KEY_EVENT_TYPE: Final = "UsbScanCodeSpecKeyEvent"
_MODIFIER_TYPE: Final = "UsbScanCodeSpecModifierType"
#: ``usbHidCode`` carries the HID usage id in the high 16 bits and the
#: keyboard usage page (7) in the low bits, as govc and PowerCLI send it.
_HID_USAGE_PAGE: Final = 7
#: Characters left unquoted in the ``/folder`` file path.
_PATH_SAFE: Final = "/"
#: The vim SOAP session cookie that vCenter's datastore file access reads.
_SOAP_SESSION_COOKIE: Final = "vmware_soap_session"
#: vCenter's answer to a file read with an expired or unknown session cookie.
_SESSION_EXPIRED_STATUS: Final = 401
#: Most folders between a datacenter and the root folder.
_MAX_FOLDER_HOPS: Final = 32
_BASE64_LINE_WIDTH: Final = 4096

#: Wall-clock bound for the screenshot task (module-global so tests can lower it).
_TASK_TIMEOUT_SECONDS = 120.0


def _envelope(status: str, vm: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "vm": vm, "vm_name": None, "guidance": None}
    out.update(fields)
    return out


async def _powered_on_vm(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm: str,
    props: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(early envelope or None, VM props)``: the VM must exist and run."""
    problem = moid_problem(vm, VM_MOID_PATTERN, "vm")
    if problem is not None:
        return _envelope("invalid_request", vm, guidance=problem), {}
    found = await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=props
    )
    if found is None:
        guidance = f"no VM {vm!r}; pass a VM id of the shape vm-<number> (vmware.vm.list finds it)"
        return _envelope("not_found", vm, guidance=guidance), {}
    name = found.get("name") if isinstance(found.get("name"), str) else None
    power = found.get(_PROP_POWER)
    if power != "poweredOn":
        guidance = f"VM {vm!r} is {power or 'not powered on'}; the console needs a running VM"
        return _envelope("refused", vm, vm_name=name, guidance=guidance), found
    return None, found


# ===========================================================================
# vm.screenshot
# ===========================================================================


def _split_datastore_path(path: Any) -> tuple[str, str] | None:
    """``"[ds] dir/file.png"`` -> ``("ds", "dir/file.png")``; ``None`` for another shape."""
    if not isinstance(path, str) or not path.startswith("["):
        return None
    name, sep, rest = path[1:].partition("]")
    rel = rest.strip().lstrip("/")
    if not sep or not name or not rel:
        return None
    return name, rel


async def _datacenter_path(
    connector: VmwareRestConnector, target: Any, operator: Operator, props: dict[str, Any]
) -> str:
    """Inventory path of the VM's datacenter (``dcPath``), such as ``"dc1"`` or ``"f/dc1"``."""
    parent = props.get("parent") or props.get("parentVApp")
    datacenter = await resolve_datacenter(connector, target, operator, parent)
    if datacenter is None:
        raise RuntimeError(f"{OP_ID_VM_SCREENSHOT}: could not find the VM's datacenter")
    names: list[str] = []
    mo_type, moid = DATACENTER_MO_TYPE, datacenter
    for _ in range(_MAX_FOLDER_HOPS):
        found = await read_object(
            connector, target, operator, mo_type=mo_type, moid=moid, props=["name", "parent"]
        )
        found = found or {}
        parent_ref = unwrap_vim_value(found.get("parent"))
        if not isinstance(parent_ref, dict) or not isinstance(parent_ref.get("value"), str):
            # The root folder: its name is not part of the path.
            return "/".join(reversed(names))
        name = found.get("name")
        if not isinstance(name, str):
            break
        names.append(name)
        mo_type, moid = str(parent_ref.get("type") or "Folder"), parent_ref["value"]
    raise RuntimeError(f"{OP_ID_VM_SCREENSHOT}: could not read the path of the VM's datacenter")


def _too_large(size: int, max_bytes: int) -> RuntimeError:
    return RuntimeError(
        f"{OP_ID_VM_SCREENSHOT}: the screenshot is {size} bytes, over the limit of "
        f"{max_bytes} bytes. Call again with a larger max_bytes (at most "
        f"{SCREENSHOT_HARD_CAP_BYTES})"
    )


async def _get_file_once(
    connector: VmwareRestConnector,
    target: Any,
    *,
    cookie: str,
    datastore_path: tuple[str, str],
    dc_path: str,
    max_bytes: int,
) -> bytes | None:
    """One ``GET /folder/...`` with *cookie*; ``None`` when vCenter answers HTTP 401."""
    datastore, rel = datastore_path
    client = await connector._http_client(target)
    async with client.stream(
        "GET",
        f"/folder/{quote(rel, safe=_PATH_SAFE)}",
        params={"dcPath": dc_path, "dsName": datastore},
        headers={"Cookie": f"{_SOAP_SESSION_COOKIE}={cookie}", "Accept-Encoding": "identity"},
        extensions=connector._request_extensions(target),
    ) as response:
        if response.status_code == _SESSION_EXPIRED_STATUS:
            return None
        if not response.is_success:
            raise RuntimeError(
                f"{OP_ID_VM_SCREENSHOT}: vCenter answered HTTP {response.status_code} to the "
                "screenshot file read"
            )
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding and encoding != "identity":
            raise RuntimeError(
                f"{OP_ID_VM_SCREENSHOT}: vCenter sent the screenshot with Content-Encoding "
                f"{encoding!r}; MEHO reads only an unencoded file"
            )
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise _too_large(int(declared), max_bytes)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_raw():
            total += len(chunk)
            if total > max_bytes:
                raise _too_large(total, max_bytes)
            chunks.append(chunk)
    return b"".join(chunks)


async def _read_datastore_file(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    datastore_path: tuple[str, str],
    dc_path: str,
    max_bytes: int,
) -> bytes:
    """GET one datastore file through vCenter, streamed and capped at *max_bytes*.

    vCenter's datastore file access (``/folder``) takes a vim SOAP session
    cookie, not the token of the REST and VI-JSON calls. The connector keeps
    one such session per target and reuses it for every file read
    (``_ensure_file_access_session``), so repeated screenshots do not each
    open a vCenter session. An HTTP 401 means that session expired: it is
    dropped, the connector logs in again, and the file is read once more.
    The answer body is never put into an error.
    """

    async def _read_once() -> bytes | None:
        cookie = await connector._ensure_file_access_session(target, operator)
        return await _get_file_once(
            connector,
            target,
            cookie=cookie,
            datastore_path=datastore_path,
            dc_path=dc_path,
            max_bytes=max_bytes,
        )

    content = await _read_once()
    if content is None:
        connector._drop_file_access_session(target)
        content = await _read_once()
    if content is None:
        raise RuntimeError(
            f"{OP_ID_VM_SCREENSHOT}: vCenter answered HTTP 401 to the screenshot file read "
            "right after a fresh login"
        )
    return content


def _max_bytes(raw: Any) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(1, min(raw, SCREENSHOT_HARD_CAP_BYTES))
    return SCREENSHOT_DEFAULT_MAX_BYTES


async def vm_screenshot_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Take a console screenshot of a running VM and return the PNG.

    Op-id: ``vmware.composite.vm.screenshot``. ``not_found`` for an unknown
    VM and ``refused`` for a VM that is not powered on, before any write.
    Otherwise the gated ``CreateScreenshot_Task`` (a parked or denied gate is
    returned as is), polled to the end; a task fault raises with the fault
    type and text. Then the PNG file is read, capped at ``max_bytes``.
    """
    vm = params["vm"]
    early, props = await _powered_on_vm(
        connector, target, operator, vm, ["name", _PROP_POWER, "parent", "parentVApp"]
    )
    if early is not None:
        return early
    envelope = _envelope("taken", vm, vm_name=props.get("name"), task=None, task_state=None)
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CREATE_SCREENSHOT_TASK,
        vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/CreateScreenshot_Task",
        body={},
        params={"vm": vm},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    envelope.update(task=outcome.task, task_state=outcome.state)
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"{OP_ID_VM_SCREENSHOT}: CreateScreenshot_Task on vm {vm!r} failed "
            f"({outcome.fault_type or 'fault'}): {outcome.error_message or '<no fault text>'}"
        )
    if outcome.timed_out:
        return {
            **envelope,
            "status": "timeout",
            "guidance": f"CreateScreenshot_Task {outcome.task} did not finish in time; try again",
            "issues": [_task_timeout_issue("CreateScreenshot_Task", outcome.task)],
        }
    file_path = unwrap_vim_value(outcome.result)
    parts = _split_datastore_path(file_path)
    if parts is None:
        raise RuntimeError(
            f"{OP_ID_VM_SCREENSHOT}: CreateScreenshot_Task returned no datastore file path"
        )
    content = await _read_datastore_file(
        connector,
        target,
        operator,
        datastore_path=parts,
        dc_path=await _datacenter_path(connector, target, operator, props),
        max_bytes=_max_bytes(params.get("max_bytes")),
    )
    b64 = base64.b64encode(content).decode("ascii")
    return {
        **envelope,
        "file": file_path,
        "size_bytes": len(content),
        "media_type": "image/png",
        "encoding": "base64",
        "content_lines": [
            b64[i : i + _BASE64_LINE_WIDTH] for i in range(0, len(b64), _BASE64_LINE_WIDTH)
        ],
    }


async def _vm_screenshot_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    vm = ctx.params.get("vm")
    if not isinstance(vm, str):
        return None
    return {
        "summary": f"Take a console screenshot of {vm}. vCenter keeps the PNG file in the "
        "VM's folder.",
        "vm": vm,
    }


# ===========================================================================
# vm.keys.send
# ===========================================================================


def _key_events(keys: list[str]) -> list[dict[str, Any]]:
    events = []
    for key in keys:
        usage, modifiers = CONSOLE_KEYS[key]
        event: dict[str, Any] = {
            VIM_TYPE_NAME_KEY: _KEY_EVENT_TYPE,
            "usbHidCode": (usage << 16) | _HID_USAGE_PAGE,
        }
        if modifiers:
            event["modifiers"] = {
                VIM_TYPE_NAME_KEY: _MODIFIER_TYPE,
                **dict.fromkeys(modifiers, True),
            }
        events.append(event)
    return events


async def vm_keys_send_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Press special keys on a running VM's console.

    Op-id: ``vmware.composite.vm.keys.send``. A key that is not on the fixed
    list answers ``invalid_request`` before any vendor call. ``not_found`` for
    an unknown VM and ``refused`` for a VM that is not powered on. Otherwise
    the gated ``PutUsbScanCodes`` (a parked or denied gate is returned as is);
    a vCenter fault raises with the fault type and text. Then the VM's power
    state is read again.
    """
    vm = params["vm"]
    keys = params.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or not all(isinstance(k, str) and k in CONSOLE_KEYS for k in keys)
    ):
        return _envelope(
            "invalid_request",
            vm,
            keys=keys if isinstance(keys, list) else None,
            guidance=f"keys must be a list of names from {sorted(CONSOLE_KEYS)}; nothing was sent",
        )
    early, props = await _powered_on_vm(connector, target, operator, vm, ["name", _PROP_POWER])
    if early is not None:
        return {**early, "keys": keys}
    envelope = _envelope("sent", vm, vm_name=props.get("name"), keys=keys)
    try:
        gate, payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_PUT_USB_SCAN_CODES,
            vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm}/PutUsbScanCodes",
            body={
                "spec": {VIM_TYPE_NAME_KEY: _SCAN_CODE_SPEC_TYPE, "keyEvents": _key_events(keys)}
            },
            params={"vm": vm, "keys": keys},
        )
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"{OP_ID_VM_KEYS_SEND}: PutUsbScanCodes on vm {vm!r} failed: "
            f"{_vsphere_fault_detail(exc)}"
        ) from exc
    if gate is not None:
        return gate
    sent = unwrap_vim_value(_unwrap_value(payload))
    after = await read_object(
        connector, target, operator, mo_type=_VIRTUAL_MACHINE_MO_TYPE, moid=vm, props=[_PROP_POWER]
    )
    return {
        **envelope,
        "keys_sent": sent if isinstance(sent, int) and not isinstance(sent, bool) else None,
        "power_state": (after or {}).get(_PROP_POWER),
    }


async def _vm_keys_send_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    vm = ctx.params.get("vm")
    keys = ctx.params.get("keys")
    if not isinstance(vm, str) or not isinstance(keys, list):
        return None
    return {
        "summary": f"Press {', '.join(map(str, keys))} on the console of {vm}.",
        "vm": vm,
        "keys": keys,
    }


#: The two registration rows; the registry collects them from this module.
COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=OP_ID_VM_SCREENSHOT,
        handler=vm_screenshot_composite,
        summary="Take a console screenshot of a running VM and return it as a PNG.",
        description=(
            "Takes a screenshot of a running VM's console and returns the PNG image as base64 "
            "rows, size-capped (default 2 MiB). vCenter keeps the PNG file in the VM's "
            "folder. Waits for approval."
        ),
        parameter_schema=VM_SCREENSHOT_PARAMETER_SCHEMA,
        response_schema=VM_SCREENSHOT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "console", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=VM_SCREENSHOT_LLM_INSTRUCTIONS,
        preview=_vm_screenshot_preview,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id=OP_ID_VM_KEYS_SEND,
        handler=vm_keys_send_composite,
        summary="Press special keys (Ctrl+Alt+Del, Enter, Esc, F1-F12, arrows) on a VM console.",
        description=(
            "Presses keys from a fixed list on a running VM's console: Ctrl+Alt+Del, Enter, "
            "Esc, Tab, Backspace, F1 to F12 and the arrow keys. No free text, so it cannot "
            "type a command. Waits for approval."
        ),
        parameter_schema=VM_KEYS_SEND_PARAMETER_SCHEMA,
        response_schema=VM_KEYS_SEND_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "console", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_KEYS_SEND_LLM_INSTRUCTIONS,
        preview=_vm_keys_send_preview,
        classifier_other=True,
    ),
)
