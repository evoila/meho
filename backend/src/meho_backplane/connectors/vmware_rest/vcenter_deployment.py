# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the vCenter install operations share (#4394).

A new vCenter is installed in two steps:

1. ``vmware.composite.vcenter.appliance.deploy`` imports the vCenter appliance
   file (OVA) from a content library of an existing vCenter. The new VM boots
   and sets up its network, then waits.
2. ``vmware.composite.vcenter.install`` runs the setup on the new appliance
   with the setup API of the appliance (``/api/vcenter/deployment``).
   ``vmware.vcenter.deployment.status`` reads how the setup goes, and
   ``vmware.composite.vcenter.deployment.answer`` answers a question the
   setup asks.

The operations of step 2 run on the **new appliance** as their own MEHO
target: the appliance address, port 5480 (the appliance management port,
which answers before the setup), and a Vault login with the root user and
the root password that step 1 set. The connector signs in with that login,
like on any vCenter target.

This module holds:

* the vendor paths, the operation group and its text;
* the reads of the setup state and the open question, and the flat views
  the operations return;
* the Vault read of a password that a call names by reference
  (:func:`read_secret_param`), and :func:`hide_secrets`, which drops a vendor
  text that holds such a password;
* :class:`StepLog`, the list of steps a multi-step change returns: which ran,
  which failed (with the vendor's error) and which did not run.
"""

from __future__ import annotations

import html
import json
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.vmware_rest.appliance_local_account_password import (
    safe_vendor_text,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "DEFAULT_PASSWORD_KEY",
    "INSTALL_GROUP_KEY",
    "INSTALL_WHEN_TO_USE",
    "MEHO_STATES",
    "OP_DEPLOYMENT_GET",
    "OP_INSTALL_CHECK",
    "OP_INSTALL_START",
    "OP_QUESTION_ANSWER",
    "OP_QUESTION_GET",
    "STATUS_OP_ID",
    "VENDOR_STATES",
    "SecretValueError",
    "StepLog",
    "deployment_view",
    "error_text",
    "hide_secrets",
    "localized_text",
    "mounted",
    "now_iso",
    "question_rows",
    "read_deployment",
    "read_questions",
    "read_secret_param",
    "status_read_ref",
    "unreachable_reason",
    "vendor_detail",
]

# Vendor calls on the new appliance, as ``METHOD:/path`` keys of ``vcenter.yaml``
# (vCenter 8.0 and 9.0 serve all five).
OP_DEPLOYMENT_GET: Final = "GET:/vcenter/deployment"
OP_QUESTION_GET: Final = "GET:/vcenter/deployment/question"
OP_INSTALL_CHECK: Final = "POST:/vcenter/deployment/install?action=check"
OP_INSTALL_START: Final = "POST:/vcenter/deployment/install?action=start"
OP_QUESTION_ANSWER: Final = "POST:/vcenter/deployment/question?action=answer"

#: The status read that follows a started install or an answer.
STATUS_OP_ID: Final = "vmware.vcenter.deployment.status"

#: One operation group for the four install operations.
INSTALL_GROUP_KEY: Final = "vcenter_install"
INSTALL_WHEN_TO_USE: Final = (
    "Use to install a new vCenter in two steps and to follow the install. "
    "Change on an existing vCenter: import the vCenter appliance file (OVA) from a "
    "content library onto a cluster or host, with its size, disk mode, network and host "
    "name (vcenter.appliance.deploy, dangerous). Changes on the new appliance, which is "
    "its own target (port 5480, root login): check the install spec and start the setup "
    "with a new SSO domain or a join to an existing one (vcenter.install, dangerous); "
    "answer a question the setup asks (vcenter.deployment.answer, dangerous). Read on the "
    "new appliance: the install state, progress, steps with their errors, and the open "
    "question (vmware.vcenter.deployment.status, safe)."
)

#: The vendor appliance states (``Vcenter.Deployment.ApplianceState``) and MEHO's words.
_STATE_WORDS: Final[dict[str, str]] = {
    "NOT_INITIALIZED": "not_ready",
    "INITIALIZED": "ready",
    "CONFIG_IN_PROGRESS": "running",
    "QUESTION_RAISED": "waiting_for_answer",
    "FAILED": "failed",
    "CONFIGURED": "done",
}
VENDOR_STATES: Final[tuple[str, ...]] = tuple(_STATE_WORDS)
#: Every ``state`` the operations return: the six above, ``unknown`` when the
#: appliance gives no state, and ``not_reachable`` when it does not answer.
MEHO_STATES: Final[tuple[str, ...]] = (*_STATE_WORDS.values(), "unknown", "not_reachable")

#: The key in a Vault secret that holds a password, unless the call names another.
DEFAULT_PASSWORD_KEY: Final = "password"

#: The text put in place of a vendor text that holds a password from Vault.
_HIDDEN_TEXT: Final = "[MEHO hid this text: it held a password from Vault]"


class SecretValueError(Exception):
    """The Vault secret a call names has no text under its key.

    The message names the parameter, the path and the key, never a value.
    """


@dataclass(slots=True)
class _SecretRefTarget:
    """Points the shared Vault read at a path the call names (not the target's login)."""

    name: str
    host: str
    secret_ref: str | None


async def read_secret_param(
    operator: Operator, target: Any, params: Mapping[str, Any], *, prefix: str
) -> str:
    """Read the password a call names with ``<prefix>_secret_ref``, under the caller's identity.

    ``<prefix>_secret_mount`` (default ``secret``) and ``<prefix>_secret_key``
    (default ``password``) are optional. A Vault refusal or a missing path
    raises :class:`~meho_backplane.connectors._shared.vault_creds.SecretRefReadError`
    (the dispatcher answers ``vault_credentials_read_error``). A secret with
    no text under the key raises :class:`SecretValueError`.
    """
    param = f"{prefix}_secret_ref"
    secret_ref = str(params.get(param) or "").strip()
    mount = str(params.get(f"{prefix}_secret_mount") or DEFAULT_KV_MOUNT).strip()
    key = str(params.get(f"{prefix}_secret_key") or DEFAULT_PASSWORD_KEY).strip()
    if not secret_ref:
        raise SecretValueError(f"{param} is empty: name the Vault path that holds the password")
    data = await load_param_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param=param,
        mount=mount,
    )
    value = data.get(key) if isinstance(data, dict) else None
    password = strip_credential_value(value) if isinstance(value, str) else ""
    if not password:
        raise SecretValueError(
            f"the Vault secret at {param}={secret_ref!r} (mount={mount!r}) has no text under "
            f"the key {key!r}. Store the password under that key, or set {prefix}_secret_key. "
            "Nothing was sent."
        )
    return password


def _secret_forms(secret: str) -> set[str]:
    return {secret, json.dumps(secret)[1:-1], quote(secret, safe=""), html.escape(secret)}


def hide_secrets(text: str | None, secrets: Iterable[str]) -> str | None:
    """*text*, or a fixed note when it holds one of *secrets* (as is or escaped)."""
    if text is None:
        return None
    for secret in secrets:
        if secret and any(form and form in text for form in _secret_forms(secret)):
            return _HIDDEN_TEXT
    return text


def vendor_detail(exc: httpx.HTTPStatusError, secrets: tuple[str, ...]) -> str:
    """``HTTP <status>: <vendor type>; <messages>``, without any text that holds a secret.

    Reads only the response. The request of *exc* (which may hold a password
    in its body) is not used.
    """
    error_type, messages = safe_vendor_text(exc.response, secrets)
    texts = [hide_secrets(text, secrets) or "" for text in (error_type, *messages) if text]
    detail = "; ".join(texts)
    return f"HTTP {exc.response.status_code}" + (f": {detail}" if detail else "")


class StepLog:
    """The steps of a multi-step change: ``done``, ``failed``, ``unknown`` or ``not_run``."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._rows: list[dict[str, Any]] = [
            {"step": name, "result": "not_run", "error": None} for name in names
        ]

    def done(self, name: str) -> None:
        self._set(name, "done", None)

    def failed(self, name: str, error: str) -> None:
        self._set(name, "failed", error)

    def unknown(self, name: str, error: str) -> None:
        """The step was sent, but its reply was lost."""
        self._set(name, "unknown", error)

    def _set(self, name: str, result: str, error: str | None) -> None:
        for row in self._rows:
            if row["step"] == name:
                row["result"], row["error"] = result, error

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


def now_iso() -> str:
    """The time now, ISO 8601 in UTC (when a read was made)."""
    return datetime.now(UTC).isoformat()


def status_read_ref(target: Any) -> dict[str, str]:
    """Where a caller follows the install: the status read on the same target."""
    return {"op_id": STATUS_OP_ID, "target": str(getattr(target, "name", ""))}


def unreachable_reason(exc: httpx.TransportError) -> str:
    """Why the appliance did not answer: ``tls_verify_failed``, ``timeout`` or ``connect_failed``.

    The appliance replaces its certificate during the setup, so a target that
    pins the factory certificate fails the TLS check part-way through.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    cause: BaseException | None = exc.__cause__ or exc.__context__
    if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return "tls_verify_failed"
    return "connect_failed"


async def mounted(
    connector: VmwareRestConnector, target: Any, operator: Operator, op_id: str
) -> str:
    """The path of *op_id* on the appliance's live REST mount (``/api``)."""
    return await connector.mount_op_path(target, op_id.partition(":")[2], operator)


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload) == {"value"}:
        return payload["value"]
    return payload


async def read_deployment(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> dict[str, Any]:
    """``GET /vcenter/deployment``: the appliance state and the setup task."""
    path = await mounted(connector, target, operator, OP_DEPLOYMENT_GET)
    info = _unwrap(await connector._get_json(target, path, operator=operator))
    return info if isinstance(info, dict) else {}


async def read_questions(
    connector: VmwareRestConnector, target: Any, operator: Operator
) -> list[dict[str, Any]]:
    """``GET /vcenter/deployment/question``: the open questions as flat rows."""
    path = await mounted(connector, target, operator, OP_QUESTION_GET)
    return question_rows(_unwrap(await connector._get_json(target, path, operator=operator)))


def localized_text(message: Any) -> str | None:
    """The English text of a vendor ``LocalizableMessage``, or ``None``."""
    if isinstance(message, dict):
        text = message.get("default_message")
        return text if isinstance(text, str) and text else None
    return message if isinstance(message, str) and message else None


def error_text(error: Any) -> str | None:
    """The text of a vendor ``error``: a text, or an error object with its ``messages``.

    The setup API gives ``error`` as an object (``error_type`` and a stack of
    ``messages``; the first describes the error, the next ones its cause).
    """
    if isinstance(error, str):
        return error or None
    if not isinstance(error, dict):
        return None
    messages = error.get("messages")
    texts = [
        text
        for text in (localized_text(m) for m in (messages if isinstance(messages, list) else []))
        if text
    ]
    if texts:
        return " ".join(texts)
    error_type = error.get("error_type")
    return error_type if isinstance(error_type, str) and error_type else None


def _progress_percent(progress: Any) -> int | None:
    if not isinstance(progress, dict):
        return None
    total, completed = progress.get("total"), progress.get("completed")
    if not isinstance(total, int) or not isinstance(completed, int) or total <= 0:
        return None
    return max(0, min(100, completed * 100 // total))


def _notification_texts(result: Any, kind: str) -> list[str]:
    """The texts of the ``errors`` / ``warnings`` / ``info`` notifications of a result."""
    entries = result.get(kind) if isinstance(result, dict) else None
    texts: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        text = localized_text(entry.get("message"))
        resolution = localized_text(entry.get("resolution"))
        if text and resolution:
            text = f"{text} Resolution: {resolution}"
        if text:
            texts.append(text)
    return texts


def _step_rows(info: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per setup step, in the vendor's order, with its error texts."""
    subtasks = info.get("subtasks")
    if not isinstance(subtasks, dict):
        return []
    order = info.get("subtask_order")
    names = [n for n in order if isinstance(n, str)] if isinstance(order, list) else []
    names += sorted(name for name in subtasks if name not in names)
    rows: list[dict[str, Any]] = []
    for name in names:
        task = subtasks.get(name)
        if not isinstance(task, dict):
            continue
        result = task.get("result")
        rows.append(
            {
                "name": name,
                "status": task.get("status") if isinstance(task.get("status"), str) else None,
                "progress_percent": _progress_percent(task.get("progress")),
                "description": localized_text(task.get("description")),
                "error": error_text(task.get("error")),
                "errors": _notification_texts(result, "errors"),
                "warnings": _notification_texts(result, "warnings"),
            }
        )
    return rows


def deployment_view(info: dict[str, Any]) -> dict[str, Any]:
    """The flat view of ``GET /vcenter/deployment`` the operations return."""
    vendor_state = info.get("state") if isinstance(info.get("state"), str) else None
    progress = info.get("progress")
    return {
        "state": _STATE_WORDS.get(vendor_state or "", "unknown"),
        "vendor_state": vendor_state,
        "task_status": info.get("status") if isinstance(info.get("status"), str) else None,
        "progress_percent": _progress_percent(progress),
        "progress_message": (
            localized_text(progress.get("message")) if isinstance(progress, dict) else None
        ),
        "error": error_text(info.get("error")),
        "operation": info.get("operation") if isinstance(info.get("operation"), str) else None,
        "started_at": info.get("start_time") if isinstance(info.get("start_time"), str) else None,
        "ended_at": info.get("end_time") if isinstance(info.get("end_time"), str) else None,
        "steps": _step_rows(info),
    }


def question_rows(payload: Any) -> list[dict[str, Any]]:
    """The open questions of ``GET /vcenter/deployment/question`` as flat rows."""
    questions = payload.get("questions") if isinstance(payload, dict) else None
    rows: list[dict[str, Any]] = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str):
            continue
        answers = question.get("possible_answers")
        default = question.get("default_answer")
        rows.append(
            {
                "id": question["id"],
                "text": localized_text(question.get("question")),
                "type": question.get("type") if isinstance(question.get("type"), str) else None,
                "default_answer": default if isinstance(default, str) else None,
                "possible_answers": [a for a in answers if isinstance(a, str)]
                if isinstance(answers, list)
                else [],
            }
        )
    return rows
