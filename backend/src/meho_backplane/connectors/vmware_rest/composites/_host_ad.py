# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Join an ESXi host to an Active Directory domain, or leave it.

``HostActiveDirectoryAuthentication.JoinDomain_Task`` and
``LeaveCurrentDomain_Task``. The object is one of the
``supportedStore`` entries of ``configManager.authenticationManager``, which
vCenter and the single host both serve. So both changes work on a vCenter
target (the call names the host) and on an ESXi target.

Each change reads the membership first (``unchanged`` sends nothing), sends
one gated task, waits for it and reads the membership again. A task fault
answers ``failed`` with the fault type and text.

The join account's password comes from Vault (``password_secret_ref``) and
goes only into the ``JoinDomain_Task`` call. The approval request, the
preview and the answer name the Vault path, never the password; a vendor text
that holds it is replaced. The join is pinned ``credential_write``
(``broadcast/events.py``), so the flight recorder keeps no request body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_access import (
    OP_JOIN_DOMAIN,
    OP_LEAVE_DOMAIN,
    AdState,
    read_ad_state,
    vendor_fault,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    hide_secrets,
    read_secret_param,
)
from meho_backplane.connectors.vmware_rest.vim_task import (
    TASK_STATE_ERROR,
    poll_vim_task,
    task_moref_value,
)
from meho_backplane.operations._preview import preview_failure_reason, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["OP_ID_AD_JOIN", "OP_ID_AD_LEAVE", "ad_join_composite", "ad_leave_composite"]

OP_ID_AD_JOIN: Final = "vmware.composite.host.ad.join"
OP_ID_AD_LEAVE: Final = "vmware.composite.host.ad.leave"

#: Wall-clock bound of the task polls (the 600 s convention); tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0

_EMPTY_STATE: Final[dict[str, Any]] = {
    "joined": None,
    "domain": None,
    "trusted_domains": [],
    "membership_status": None,
}


def _same_domain(left: str | None, right: str) -> bool:
    return left is not None and left.casefold() == right.casefold()


def _answer(
    status: str,
    state: AdState | None,
    host: Any,
    *,
    task: str | None = None,
    guidance: str | None = None,
    error: tuple[str | None, str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    fields = state.as_fields() if state is not None else {"host": host, **_EMPTY_STATE}
    answer: dict[str, Any] = {
        "status": status,
        **fields,
        **extra,
        "task": task,
        "error_type": error[0] if error else None,
        "error_message": error[1] if error else None,
        "guidance": guidance,
    }
    if error is not None:
        answer["issues"] = [issue("connector", "error", f"{error[0] or 'fault'}: {error[1]}")]
    return answer


async def _run_task(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    plan: dict[str, Any],
    secrets: tuple[str, ...],
) -> tuple[OperationResult | None, str | None, tuple[str | None, str] | None, bool]:
    """Gate, send and wait for one task: ``(gate, task id, fault, timed out)``."""
    try:
        gate, payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=vmomi_path,
            body=body,
            params=plan,
        )
    except (httpx.HTTPStatusError, RuntimeError) as exc:
        fault = vendor_fault(exc, secrets)
        if fault is None:
            raise
        return None, None, fault, False
    if gate is not None:
        return gate, None, None, False
    task = _unwrap_value(payload)
    outcome = await poll_vim_task(
        connector, target, operator, task=task, timeout_seconds=_TASK_TIMEOUT_SECONDS
    )
    if outcome.state == TASK_STATE_ERROR:
        text = hide_secrets(outcome.error_message, secrets) or "<no fault text reported>"
        return None, outcome.task, (outcome.fault_type, text), False
    return None, outcome.task or task_moref_value(task), None, bool(outcome.timed_out)


def _refusal(refusal: dict[str, Any], host: Any) -> dict[str, Any]:
    status = str(refusal.get("status") or "host_not_found")
    return {**_answer(status, None, host, guidance=refusal.get("guidance")), **refusal}


def _no_store(state: AdState) -> dict[str, Any]:
    return _answer(
        "not_supported",
        state,
        state.host,
        guidance="The host offers no Active Directory store. Nothing was sent.",
    )


def _timeout(state: AdState, task: str | None, method: str, **extra: Any) -> dict[str, Any]:
    text = f"MEHO stopped waiting for {method} {task}; it may still run. Read the host again."
    answer = _answer("timeout", state, state.host, task=task, guidance=text, **extra)
    answer["issues"] = [issue("connector", "error", text)]
    return answer


async def _after(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    before: AdState,
    task: str | None,
    done: str,
    is_done: Any,
    **extra: Any,
) -> dict[str, Any]:
    """Read the membership again: *done* when ``is_done(state)``, else not_verified."""
    after, _refusal_after = await read_ad_state(connector, target, operator, before.host)
    if after is not None and is_done(after):
        return _answer(done, after, before.host, task=task, **extra)
    text = "The task ended, but the host does not read back as asked. Read it again."
    answer = _answer(
        "not_verified", after or before, before.host, task=task, guidance=text, **extra
    )
    answer["issues"] = [issue("connector", "error", text)]
    return answer


# ---------------------------------------------------------------------------
# host.ad.join
# ---------------------------------------------------------------------------


def _join_plan(params: dict[str, Any], host: str | None) -> dict[str, Any]:
    """What the join does, without the password: for the approver and the gate."""
    return {
        "action": "join the host to the Active Directory domain",
        "host": host or params.get("host"),
        "domain": params["domain"],
        "user_name": params["user_name"],
        "password_source": "vault",
        "password_secret_ref": params.get("password_secret_ref"),
    }


async def ad_join_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Join the host to a domain (``JoinDomain_Task``)."""
    domain = params["domain"]
    state, refusal = await read_ad_state(connector, target, operator, params.get("host"))
    if state is None:
        return {**_refusal(refusal or {}, params.get("host")), "requested_domain": domain}
    if state.store is None:
        return {**_no_store(state), "requested_domain": domain}
    if state.joined and _same_domain(state.domain, domain):
        return _answer(
            "unchanged",
            state,
            state.host,
            guidance="The host already is in this domain. Nothing was sent.",
            requested_domain=domain,
        )
    if state.joined:
        return _answer(
            "joined_other_domain",
            state,
            state.host,
            guidance="The host is in another domain. Nothing was sent. Leave it first "
            "(host.ad.leave), then join.",
            requested_domain=domain,
        )
    password = await read_secret_param(operator, target, params, prefix="password")
    gate, task, fault, timed_out = await _run_task(
        connector,
        target,
        operator,
        op_id=OP_JOIN_DOMAIN,
        vmomi_path=f"/HostActiveDirectoryAuthentication/{state.store}/JoinDomain_Task",
        body={"domainName": domain, "userName": params["user_name"], "password": password},
        plan=_join_plan(params, state.host),
        secrets=(password,),
    )
    if gate is not None:
        return gate
    if fault is not None:
        return _answer(
            "failed",
            state,
            state.host,
            task=task,
            error=fault,
            guidance="The host did not join; nothing changed. Fix the cause the error names "
            "and call again.",
            requested_domain=domain,
        )
    if timed_out:
        return _timeout(state, task, "JoinDomain_Task", requested_domain=domain)
    return await _after(
        connector,
        target,
        operator,
        before=state,
        task=task,
        done="joined",
        is_done=lambda after: after.joined and _same_domain(after.domain, domain),
        requested_domain=domain,
    )


# ---------------------------------------------------------------------------
# host.ad.leave
# ---------------------------------------------------------------------------


async def ad_leave_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Take the host out of its domain (``LeaveCurrentDomain_Task``)."""
    force = params.get("force") is True
    state, refusal = await read_ad_state(connector, target, operator, params.get("host"))
    if state is None:
        return _refusal(refusal or {}, params.get("host"))
    if state.store is None:
        return _no_store(state)
    if not state.joined:
        return _answer(
            "unchanged",
            state,
            state.host,
            guidance="The host is in no domain. Nothing was sent.",
        )
    gate, task, fault, timed_out = await _run_task(
        connector,
        target,
        operator,
        op_id=OP_LEAVE_DOMAIN,
        vmomi_path=f"/HostActiveDirectoryAuthentication/{state.store}/LeaveCurrentDomain_Task",
        body={"force": force},
        plan={
            "action": "leave the Active Directory domain",
            "host": state.host,
            "domain": state.domain,
            "force": force,
        },
        secrets=(),
    )
    if gate is not None:
        return gate
    if fault is not None:
        return _answer(
            "failed",
            state,
            state.host,
            task=task,
            error=fault,
            guidance="The host is still in the domain. If domain users have permissions on "
            "it, remove them or call again with force true.",
        )
    if timed_out:
        return _timeout(state, task, "LeaveCurrentDomain_Task")
    return await _after(
        connector,
        target,
        operator,
        before=state,
        task=task,
        done="left",
        is_done=lambda after: not after.joined,
    )


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------

_WHAT_HAPPENS: Final[dict[str, str]] = {
    OP_ID_AD_JOIN: (
        "The host joins the domain with the join account. Then domain users and groups can "
        "get permissions on the host. The password is read from Vault when the call runs."
    ),
    OP_ID_AD_LEAVE: (
        "The host leaves the domain. Domain users cannot log in to it any more. With force, "
        "the permissions of domain users and groups on the host are deleted too."
    ),
}


async def _ad_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The membership now and what the change does; never the password. Never blocks."""
    if ctx.connector_instance is None:
        return None
    op_id = getattr(ctx.descriptor, "op_id", None)
    params = dict(ctx.params)
    preview: dict[str, Any] = {"host": params.get("host")}
    if isinstance(params.get("domain"), str) and isinstance(params.get("user_name"), str):
        preview.update(_join_plan(params, None))
    if "force" in params:
        preview["force"] = params["force"] is True
    try:
        state, refusal = await read_ad_state(
            ctx.connector_instance,  # type: ignore[arg-type]
            ctx.target,
            ctx.operator,
            params.get("host"),
        )
    except Exception as exc:
        preview["unavailable"] = [f"the domain membership: {preview_failure_reason(exc)}"]
    else:
        if state is not None:
            preview["current"] = state.as_fields()
        else:
            preview["unavailable"] = [f"the host: {(refusal or {}).get('status')}"]
    if isinstance(op_id, str) and op_id in _WHAT_HAPPENS:
        preview["what_happens_next"] = _WHAT_HAPPENS[op_id]
    return preview


register_preview_builder(OP_ID_AD_JOIN, _ad_preview)
register_preview_builder(OP_ID_AD_LEAVE, _ad_preview)
