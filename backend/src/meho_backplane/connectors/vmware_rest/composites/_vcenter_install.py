# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Step 2 of a vCenter install: ``vmware.composite.vcenter.install`` (#4394).

It runs on the **new appliance** as its own target (port 5480, root login;
see :mod:`..vcenter_deployment`). ``dangerous`` with ``requires_approval=True``.

1. ``GET /vcenter/deployment``: the setup starts only from the vendor state
   ``INITIALIZED`` (MEHO: ``ready``). A running setup is not started again:
   the answer is ``already_running``. Any other state stops with that state.
2. The SSO admin password is read from Vault (``sso_admin_password_secret_ref``).
3. ``POST /vcenter/deployment/install?action=check``: a failed check returns
   the check messages and starts nothing. When the check call itself fails,
   ``check.errors`` holds the vendor's text (never the password). The check
   and the start send the ``InstallSpec`` in a ``{"spec": ...}`` body (see
   :func:`~meho_backplane.connectors.vmware_rest.vcenter_deployment.setup_body`).
4. ``POST /vcenter/deployment/install?action=start`` after the governed write
   gate. The vendor answers at once and runs the setup by itself. The answer
   is ``accepted`` with the status read that follows the setup.

The vendor gives no job id for the setup: an appliance runs one setup, so the
job is named by the appliance target. A lost reply of the start gives
``start_unknown``: the caller reads ``vmware.vcenter.deployment.status``
before it tries again.

The SSO admin password is only in the check and start bodies. A vendor error
of these calls never leaves this module with its request (which holds the
password): the answer keeps only the vendor messages, and drops a message
that holds the password. The op is pinned as ``credential_write``, so the flight
recorder keeps no request body and the dispatcher keeps no vendor text of a
failed call. A 401 on the check of a new SSO domain means the root login
expired: it is raised again without its request, so the dispatcher logs in
again and runs the call once more. When joining an SSO domain, a 401 means
the partner vCenter refused the SSO login, and the check fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    sanitized_status_error,
)
from meho_backplane.connectors.vmware_rest.composite_issues import issue
from meho_backplane.connectors.vmware_rest.composites._write import (
    _WRITE_REQUIRES_APPROVAL,
    _WRITE_SAFETY_LEVEL,
    _step_connector_id,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import (
    OP_DEPLOYMENT_GET,
    OP_INSTALL_CHECK,
    OP_INSTALL_START,
    StepLog,
    deployment_view,
    hide_secrets,
    localized_text,
    mounted,
    read_deployment,
    read_secret_param,
    setup_body,
    status_read_ref,
    unreachable_reason,
    vendor_detail,
)
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = ["INSTALL_OP_ID", "vcenter_install_composite", "vcenter_install_preview"]

INSTALL_OP_ID: Final = "vmware.composite.vcenter.install"

_SUB_OPS_VCENTER_INSTALL: tuple[str, ...] = (OP_DEPLOYMENT_GET, OP_INSTALL_CHECK, OP_INSTALL_START)

_STEPS: Final = ("read_state", "read_sso_password", "check", "start")

#: Answer for each appliance state in which the setup is not started.
_NOT_STARTABLE: Final[dict[str, tuple[str, str]]] = {
    "running": (
        "already_running",
        "The setup already runs on this appliance. MEHO started nothing. Follow it with "
        "vmware.vcenter.deployment.status.",
    ),
    "waiting_for_answer": (
        "waiting_for_answer",
        "The setup already runs and waits for an answer. MEHO started nothing. Read the "
        "question with vmware.vcenter.deployment.status and answer it with "
        "vmware.composite.vcenter.deployment.answer.",
    ),
    "done": ("already_done", "The setup of this appliance already ran. MEHO started nothing."),
    "failed": (
        "failed_before",
        "An earlier setup of this appliance failed. MEHO started nothing. Read the steps with "
        "vmware.vcenter.deployment.status, fix the cause and deploy a new appliance.",
    ),
    "not_ready": (
        "not_ready",
        "The appliance has not finished its first start. MEHO started nothing. Read "
        "vmware.vcenter.deployment.status again in a few minutes; start when it says ready.",
    ),
    "unknown": ("unknown_state", "The appliance gave no install state. MEHO started nothing."),
}
#: States in which a setup already runs: a help to the caller, not a failed call.
_RUNNING: Final = frozenset({"already_running", "waiting_for_answer"})


def _mode(params: dict[str, Any]) -> str:
    return "join_sso_domain" if isinstance(params.get("replicated"), dict) else "new_sso_domain"


def _sso_domain_name(params: dict[str, Any]) -> str | None:
    standalone = params.get("standalone")
    domain = standalone.get("sso_domain_name") if isinstance(standalone, dict) else None
    return domain if isinstance(domain, str) and domain else None


def _install_spec(params: dict[str, Any], password: str) -> dict[str, Any]:
    """The vendor ``InstallSpec`` of the check and the start (sent in :func:`setup_body`)."""
    embedded: dict[str, Any] = {"ceip_enabled": bool(params.get("ceip_enabled", False))}
    replicated = params.get("replicated")
    if isinstance(replicated, dict):
        partner: dict[str, Any] = {
            "partner_hostname": str(replicated["partner_hostname"]),
            "sso_admin_password": password,
        }
        for key in ("https_port", "ssl_thumbprint", "ssl_verify"):
            if key in replicated:
                partner[key] = replicated[key]
        embedded["replicated"] = partner
    else:
        standalone: dict[str, Any] = {"sso_admin_password": password}
        domain = _sso_domain_name(params)
        if domain:
            standalone["sso_domain_name"] = domain
        embedded["standalone"] = standalone
    return {"auto_answer": bool(params.get("auto_answer", False)), "vcsa_embedded": embedded}


def _install_plan(params: dict[str, Any]) -> dict[str, Any]:
    """What the install does, without the password: for the approver and the gate."""
    plan: dict[str, Any] = {
        "mode": _mode(params),
        "ceip_enabled": bool(params.get("ceip_enabled", False)),
        "auto_answer": bool(params.get("auto_answer", False)),
        "sso_admin_password_source": "vault",
        "sso_admin_password_secret_ref": params.get("sso_admin_password_secret_ref"),
    }
    replicated = params.get("replicated")
    if isinstance(replicated, dict):
        plan["partner_hostname"] = replicated.get("partner_hostname")
        plan["partner_https_port"] = replicated.get("https_port", 443)
    else:
        plan["sso_domain_name"] = _sso_domain_name(params) or "vsphere.local"
    return plan


def _notification_texts(result: dict[str, Any], kind: str, secrets: tuple[str, ...]) -> list[str]:
    texts: list[str] = []
    for entry in result.get(kind) or []:
        if not isinstance(entry, dict):
            continue
        text = localized_text(entry.get("message"))
        fix = localized_text(entry.get("resolution"))
        if text:
            texts.append(hide_secrets(f"{text} Resolution: {fix}" if fix else text, secrets) or "")
    return texts


def _check_result(payload: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    """The check answer (``CheckInfo``) as ``{status, errors, warnings, info}``."""
    info: dict[str, Any] = payload if isinstance(payload, dict) else {}
    raw = info.get("result")
    result: dict[str, Any] = raw if isinstance(raw, dict) else {}
    status = info.get("status")
    return {
        "status": status if isinstance(status, str) else None,
        "errors": _notification_texts(result, "errors", secrets),
        "warnings": _notification_texts(result, "warnings", secrets),
        "info": _notification_texts(result, "info", secrets),
    }


def _empty_check(status: str | None) -> dict[str, Any]:
    return {"status": status, "errors": [], "warnings": [], "info": []}


@dataclass
class _Run:
    """One install call: its inputs and what it found so far."""

    target: Any
    params: dict[str, Any]
    steps: StepLog = field(default_factory=lambda: StepLog(_STEPS))
    view: dict[str, Any] | None = None
    check: dict[str, Any] | None = None

    def answer(
        self, status: str, guidance: str, error: str | None = None, severity: str = "error"
    ) -> dict[str, Any]:
        return {
            "status": status,
            "mode": _mode(self.params),
            "state": self.view["state"] if self.view else None,
            "vendor_state": self.view["vendor_state"] if self.view else None,
            "check": self.check,
            "job": {"kind": "install", "target": str(getattr(self.target, "name", ""))},
            "status_read": status_read_ref(self.target),
            "steps": self.steps.rows(),
            "issues": [issue("install", severity, error)] if error else [],
            "guidance": guidance,
        }


async def _read_state(
    connector: VmwareRestConnector, operator: Operator, run: _Run
) -> dict[str, Any] | None:
    """Step 1. ``None`` when the appliance is ready; else the answer that stops the call."""
    try:
        run.view = deployment_view(await read_deployment(connector, run.target, operator))
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        run.steps.failed("read_state", f"the appliance did not answer ({reason})")
        return run.answer(
            "not_reachable",
            "Check the target address and port 5480, then try again.",
            f"the appliance did not answer ({reason}); nothing was started",
        )
    run.steps.done("read_state")
    if run.view["vendor_state"] == "INITIALIZED":
        return None
    status, guidance = _NOT_STARTABLE.get(run.view["state"], _NOT_STARTABLE["unknown"])
    return run.answer(
        status,
        guidance,
        f"the appliance state is {run.view['vendor_state']!r}; nothing was started",
        severity="warning" if status in _RUNNING else "error",
    )


async def _post_check(
    connector: VmwareRestConnector,
    operator: Operator,
    run: _Run,
    body: dict[str, Any],
    secrets: tuple[str, ...],
) -> str | None:
    """Step 3. Sets ``run.check``; returns why the check failed, or ``None``."""
    path = await mounted(connector, run.target, operator, OP_INSTALL_CHECK)
    relogin: httpx.HTTPStatusError | None = None
    failure: str | None = None
    try:
        payload = await connector._post_json(run.target, path, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        run.check = _empty_check("FAILED")
        if exc.response.status_code == 401 and _mode(run.params) == "new_sso_domain":
            relogin = sanitized_status_error(exc, secrets=secrets)
        else:
            failure = vendor_detail(exc, secrets)
            if exc.response.status_code == 401:
                failure += (
                    " (the partner vCenter refused the SSO login: check partner_hostname and "
                    "the SSO admin password in Vault)"
                )
    except httpx.TransportError as exc:
        run.check = _empty_check(None)
        failure = f"the check got no answer ({unreachable_reason(exc)})"
    else:
        run.check = _check_result(payload, secrets)
        if run.check["status"] != "SUCCESS" or run.check["errors"]:
            failure = "; ".join(run.check["errors"]) or f"check status {run.check['status']!r}"
    if relogin is not None:
        # Raised outside the except block, so the error keeps no request body.
        raise relogin
    if failure is not None and not run.check["errors"]:
        # The check call failed: the caller finds the reason in check.errors too.
        run.check["errors"] = [failure]
    return failure


async def _post_start(
    connector: VmwareRestConnector,
    operator: Operator,
    run: _Run,
    body: dict[str, Any],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    """Step 4, after the gate: send the start and say what happened."""
    path = await mounted(connector, run.target, operator, OP_INSTALL_START)
    try:
        await connector._post_json(run.target, path, operator=operator, json=body)
    except httpx.HTTPStatusError as exc:
        detail = vendor_detail(exc, secrets)
        run.steps.failed("start", detail)
        return run.answer(
            "start_failed",
            "Read vmware.vcenter.deployment.status to see the state before you try again.",
            f"the appliance refused the start: {detail}",
        )
    except httpx.TransportError as exc:
        reason = unreachable_reason(exc)
        run.steps.unknown("start", f"the start got no answer ({reason})")
        return run.answer(
            "start_unknown",
            "Do not start again blindly. Read vmware.vcenter.deployment.status: running or "
            "waiting_for_answer means the start worked; ready means it did not.",
            f"the start got no answer ({reason}); MEHO does not know if the setup runs",
        )
    run.steps.done("start")
    return run.answer(
        "accepted",
        "The appliance accepted the start and runs the setup by itself; it is not done yet. "
        "Read vmware.vcenter.deployment.status on this target until the state is done or "
        "failed. The setup often takes 20 to 60 minutes.",
    )


async def vcenter_install_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Check the install spec on a new appliance, then start its setup.

    Op-id: ``vmware.composite.vcenter.install``. See the module docstring for
    the flow. A failed check returns ``check_failed`` and starts nothing. A
    parked or denied start gate comes back as is.
    """
    run = _Run(target=target, params=params)
    stop = await _read_state(connector, operator, run)
    if stop is not None:
        return stop
    password = await read_secret_param(operator, target, params, prefix="sso_admin_password")
    run.steps.done("read_sso_password")
    secrets = (password,)
    body = setup_body(_install_spec(params, password))

    failure = await _post_check(connector, operator, run, body, secrets)
    if failure is not None:
        run.steps.failed("check", failure)
        return run.answer(
            "check_failed",
            "Fix the spec or the systems the check names, then call this again.",
            f"the install check failed: {failure}; nothing was started",
        )
    run.steps.done("check")

    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_step_connector_id(connector),
        op_id=OP_INSTALL_START,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=_install_plan(params),
    )
    if gate is not None:
        return gate
    return await _post_start(connector, operator, run, body, secrets)


async def vcenter_install_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees: the install plan, never the SSO password."""
    if not isinstance(ctx.params.get("sso_admin_password_secret_ref"), str):
        return None
    return {
        "action": "set up the new vCenter appliance of this target",
        "target": str(getattr(ctx.target, "name", "")),
        **_install_plan(ctx.params),
        "note": (
            "The setup starts only if the appliance state is ready and the vendor check "
            "passes. It cannot be undone."
        ),
    }
