# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Orchestrator changes: run a workflow, and cancel a workflow run.

Each function is the body of a thin shim method on
:class:`~meho_backplane.connectors.vro.connector.VroConnector`; the op
metadata lives in :mod:`meho_backplane.connectors.vro.write_ops`.

Run a workflow (``vro.workflow.run``, ``destructive``)
------------------------------------------------------

A workflow can do anything, so only a person runs it, after a preview and
a second person's approval. The flow:

1. The preview builder (:func:`_workflow_run_preview`) reads the workflow
   (``vro.workflow.get``: version, ``content_sha256`` and the declared
   inputs), checks the inputs and returns the approval card: the workflow,
   the inputs (a secret input shows its Vault reference, never a value),
   ``run_sha256`` and the ``blast_radius`` block. It refuses (raises) when
   the workflow does not exist or an input is wrong, so the call cannot
   wait for approval.
2. The approved run reads the workflow again and computes ``run_sha256``
   again. When it differs from the value on the card (the workflow
   changed, or the inputs differ) it returns ``precondition_failed`` and
   starts nothing. It also stops when no approval card is bound.
3. Unless ``allow_parallel`` is true, it reads the runs of the workflow.
   When one is still running or waiting it returns ``already_running``
   with that run's id and starts nothing. This is a help, not a lock.
4. It reads each secret input from Vault (``secret_ref``,
   ``"<vault-path>#<field>"``) under the caller's identity, and sends
   ``POST /vco/api/workflows/{workflowId}/executions`` once (no retry).
   The params hold only Vault references for secret inputs, never a value,
   so the approval row and the broadcast hold no secret value. The flight
   recorder keeps no request body of this call, and the op's
   ``destructive`` tag makes it keep no body of any call of the run. A
   vendor error comes back with the vendor's text, so a person learns why
   the run was refused; when that text holds a secret input value, the
   answer comes back without the text.
5. The answer says ``accepted`` with the run id (from the answer body, or
   the end of the ``Location`` header). The run is not done: the caller
   reads ``vro.execution.get`` until it ends.

``run_sha256`` is the SHA-256 of a fixed JSON form of: the workflow id,
its version, its ``content_sha256``, the inputs (name, type and value; a
secret input gives its reference, never its value) and ``allow_parallel``.

Input values
------------

The caller gives ``{name, value}`` or ``{name, secret_ref}``. The type
comes from the workflow's own declaration. The value is wrapped the way
the vendor expects: ``{"string": {"value": ...}}``, ``number``,
``boolean``, ``date``, ``secure-string``, ``array`` (``Array/<type>``) and
``sdk-object`` (a plug-in type such as ``VC:VirtualMachine``; the value is
the object id). Other types are refused with a clear message.

Every input type that holds a secret is treated as secret: a type whose
name contains ``SecureString`` or ``EncryptedString`` in any letter case,
also inside a list (``Array/...``) or a composite type
(``CompositeType(...)``). This is the same rule the reads use to hide
values. Such an input never takes a value, because a value would show on
the approval card. Only an input of exactly type ``SecureString`` takes a
``secret_ref``; every other type that holds a secret is refused, because
one ``secret_ref`` cannot fill it. An input with a secret name takes only a
``secret_ref`` too.

Cancel a run (``vro.execution.cancel``, ``dangerous``)
------------------------------------------------------

Reads the run state (``GET .../state``). A run that already ended is
``unchanged``; nothing is sent. Otherwise sends
``DELETE /vco/api/workflows/{workflowId}/executions/{executionId}/state``
and answers ``cancel_requested``. Safe to run twice.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.vro.session import VroTargetLike
from meho_backplane.connectors.vro.typed_reads import (
    OUTCOME_BY_STATE,
    flat_rows,
    is_secret_name,
    path_segment,
    vro_workflow_get_impl,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vro.connector import VroConnector

__all__ = [
    "CANCEL_OP_ID",
    "RUN_OP_ID",
    "WRITE_VENDOR_CALLS",
    "run_sha256",
    "vro_execution_cancel_impl",
    "vro_workflow_run_impl",
    "wrap_value",
]

RUN_OP_ID: Final = "vro.workflow.run"
CANCEL_OP_ID: Final = "vro.execution.cancel"
STATUS_READ_OP_ID: Final = "vro.execution.get"

_EXECUTIONS_PATH: Final = "/vco/api/workflows/{workflowId}/executions"
_STATE_PATH: Final = "/vco/api/workflows/{workflowId}/executions/{executionId}/state"

#: Every vendor call the changes add, as ``(method, path template)``. The
#: run also reads the workflow and its runs, with the read calls.
WRITE_VENDOR_CALLS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", _EXECUTIONS_PATH),
        ("GET", _STATE_PATH),
        ("DELETE", _STATE_PATH),
    }
)

#: Runs read for the "already running" check.
_RUNNING_CHECK_ROWS: Final = 200
#: Plain outcomes of a run that has not ended.
_ACTIVE_OUTCOMES: Final[frozenset[str]] = frozenset({"running", "waiting"})
#: Plain outcomes of a run that has ended.
_ENDED_OUTCOMES: Final[frozenset[str]] = frozenset({"completed", "failed", "canceled"})

_SCALAR_KEYS: Final[dict[str, str]] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "Date": "date",
    "SecureString": "secure-string",
}
#: Words that mark a type as holding a secret, anywhere in the type name, any case.
_SECRET_TYPE_WORDS: Final[tuple[str, ...]] = ("securestring", "encryptedstring")
#: The one secret type MEHO can send, always from a Vault reference.
_SENDABLE_SECRET_TYPE: Final = "SecureString"
#: Declared types an input may fill from a Vault reference.
_SECRET_REF_TYPES: Final[frozenset[str]] = frozenset({"SecureString", "string"})
_ARRAY_PREFIX: Final = "Array/"
_VENDOR_TEXT_MAX: Final = 1000
_WITHHELD: Final = "the vendor error text was withheld because it held a secret input value"


class VroRunRefusedError(ValueError):
    """The run cannot start as asked; the message says why. Nothing was sent."""


def _is_secret_type(type_name: str) -> bool:
    """True for a type that holds a secret anywhere in it (same rule as the reads)."""
    lowered = type_name.lower()
    return any(word in lowered for word in _SECRET_TYPE_WORDS)


def wrap_value(type_name: str, value: Any, *, name: str) -> dict[str, Any]:
    """Wrap *value* the way the Orchestrator expects for *type_name*.

    Raises :class:`VroRunRefusedError` for a value that does not fit the
    type, or a type MEHO cannot send.
    """
    if type_name.startswith(_ARRAY_PREFIX):
        if not isinstance(value, list):
            raise VroRunRefusedError(f"input {name!r} has type {type_name}: give a list")
        element_type = type_name[len(_ARRAY_PREFIX) :]
        return {"array": {"elements": [wrap_value(element_type, v, name=name) for v in value]}}
    if ":" in type_name:
        if not isinstance(value, str) or not value:
            raise VroRunRefusedError(
                f"input {name!r} has the plug-in type {type_name}: give the object id as text"
            )
        return {"sdk-object": {"type": type_name, "id": value}}
    key = _SCALAR_KEYS.get(type_name)
    if key is None:
        raise VroRunRefusedError(
            f"input {name!r} has type {type_name}, which MEHO cannot send. Supported: "
            "string, number, boolean, Date, SecureString, Array/<type> and plug-in types."
        )
    fits = {
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(type_name, isinstance(value, str))
    if not fits:
        raise VroRunRefusedError(f"input {name!r} has type {type_name}: the value does not fit")
    return {key: {"value": value}}


@dataclass(frozen=True)
class _RunInput:
    """One checked input: its declared type, and a value or a Vault reference."""

    name: str
    type: str
    value: Any = None
    secret_ref: str | None = None

    def card(self) -> dict[str, Any]:
        """The input as the approval card and ``run_sha256`` show it: never a secret value."""
        if self.secret_ref is not None:
            return {"name": self.name, "type": self.type, "secret_ref": self.secret_ref}
        return {"name": self.name, "type": self.type, "value": self.value}


def _parse_secret_ref(raw: Any, *, name: str) -> tuple[str, str]:
    """``(vault_path, field)`` of a ``"<vault-path>#<field>"`` reference."""
    if not isinstance(raw, str) or raw.count("#") != 1:
        raise VroRunRefusedError(
            f"input {name!r}: secret_ref must be one '<vault-path>#<field>' reference"
        )
    path, _, field = raw.partition("#")
    if not path.strip() or not field.strip() or any(ch < " " for ch in raw):
        raise VroRunRefusedError(
            f"input {name!r}: secret_ref must give a vault path and a field, '<vault-path>#<field>'"
        )
    return path.strip(), field.strip()


def _check_inputs(raw_inputs: Any, declared: list[dict[str, Any]]) -> list[_RunInput]:
    """Check the caller's inputs against the workflow's declared inputs."""
    types = {d.get("name"): d.get("type") for d in declared if isinstance(d.get("name"), str)}
    checked: list[_RunInput] = []
    seen: set[str] = set()
    for raw in raw_inputs or []:
        if not isinstance(raw, dict):
            raise VroRunRefusedError("each input must be an object {name, value or secret_ref}")
        name = raw.get("name")
        if not isinstance(name, str) or name not in types:
            raise VroRunRefusedError(
                f"the workflow has no input named {name!r}; vro.workflow.get lists its inputs"
            )
        if name in seen:
            raise VroRunRefusedError(f"input {name!r} is given twice")
        seen.add(name)
        type_name = str(types[name])
        secret_type = _is_secret_type(type_name)
        if secret_type and type_name != _SENDABLE_SECRET_TYPE:
            raise VroRunRefusedError(
                f"input {name!r} has type {type_name}, which holds a secret. MEHO cannot "
                "send it: a secret value would show on the approval card, and a "
                "secret_ref fills only an input of type SecureString."
            )
        if "secret_ref" in raw:
            if type_name not in _SECRET_REF_TYPES:
                raise VroRunRefusedError(
                    f"input {name!r} has type {type_name}; only a SecureString or string "
                    "input takes a secret_ref"
                )
            path, field = _parse_secret_ref(raw["secret_ref"], name=name)
            checked.append(_RunInput(name=name, type=type_name, secret_ref=f"{path}#{field}"))
            continue
        if secret_type or is_secret_name(name):
            raise VroRunRefusedError(
                f"input {name!r} is a secret: give secret_ref, the Vault place of the "
                "value, not the value"
            )
        wrap_value(type_name, raw.get("value"), name=name)
        checked.append(_RunInput(name=name, type=type_name, value=raw.get("value")))
    return sorted(checked, key=lambda item: item.name)


def run_sha256(workflow: dict[str, Any], inputs: list[_RunInput], *, allow_parallel: bool) -> str:
    """SHA-256 over the workflow version and content and the inputs; no secret value."""
    fixed = json.dumps(
        {
            "workflow_id": workflow.get("id"),
            "version": workflow.get("version"),
            "content_sha256": workflow.get("content_sha256"),
            "inputs": [item.card() for item in inputs],
            "allow_parallel": allow_parallel,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(fixed.encode("utf-8")).hexdigest()


async def _read_and_check(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> tuple[dict[str, Any], list[_RunInput], str]:
    """Read the workflow, check the inputs; return ``(workflow, inputs, run_sha256)``."""
    workflow_id = params.get("workflow_id")
    path_segment("workflow", workflow_id)
    try:
        workflow = await vro_workflow_get_impl(
            connector, operator, target, {"workflow_id": workflow_id}
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != httpx.codes.NOT_FOUND:
            raise
        raise VroRunRefusedError(
            f"Orchestrator has no workflow with id {workflow_id!r} that this login may read. "
            "Nothing runs; vro.workflow.list finds workflows."
        ) from None
    inputs = _check_inputs(params.get("inputs"), workflow.get("inputs") or [])
    digest = run_sha256(workflow, inputs, allow_parallel=params.get("allow_parallel") is True)
    return workflow, inputs, digest


def _plugin_objects(inputs: list[_RunInput]) -> list[dict[str, Any]]:
    """The plug-in objects the run is given (``sdk-object`` inputs), for ``children``."""
    objects: list[dict[str, Any]] = []
    for item in inputs:
        element_type = item.type.removeprefix(_ARRAY_PREFIX)
        if ":" not in element_type or item.secret_ref is not None:
            continue
        values = item.value if isinstance(item.value, list) else [item.value]
        objects.extend(
            {"kind": element_type, "id": v, "input": item.name} for v in values if v is not None
        )
    return objects


async def _workflow_run_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``vro.workflow.run``: the approval card. Read-only; raises to refuse.

    Declines (``None``) without a live connector or target: the preview then
    has no card, and the destructive gate refuses the call.
    """
    from meho_backplane.connectors.vro.connector import VroConnector

    connector = ctx.connector_instance
    if not isinstance(connector, VroConnector) or ctx.target is None:
        return None
    workflow, inputs, digest = await _read_and_check(
        connector, ctx.operator, ctx.target, ctx.params
    )
    return {
        "workflow": {
            "id": workflow.get("id"),
            "name": workflow.get("name"),
            "version": workflow.get("version"),
            "content_sha256": workflow.get("content_sha256"),
        },
        "inputs": [item.card() for item in inputs],
        "allow_parallel": ctx.params.get("allow_parallel") is True,
        "run_sha256": digest,
        "blast_radius": {
            "object": {
                "kind": "vro-workflow",
                "id": workflow.get("id"),
                "name": workflow.get("name"),
                "version": workflow.get("version"),
            },
            "children": _plugin_objects(inputs),
            "irreversibility": (
                "unknown: the workflow's own code decides what it changes, and MEHO "
                "cannot undo it. Cancelling a run stops it but does not undo the steps "
                "that already ran."
            ),
        },
    }


@dataclass
class _SecretRefTarget:
    """The target shape the Vault read needs for one ``secret_ref``.

    Not frozen: the structural Protocol of the shared Vault read declares
    settable members.
    """

    name: str
    host: str
    secret_ref: str | None


async def _read_secret(target: VroTargetLike, operator: Operator, item: _RunInput) -> str:
    """Read the value of *item*'s ``secret_ref`` under the caller's identity."""
    path, _, field = str(item.secret_ref).partition("#")
    data = await load_param_secret_data(
        _SecretRefTarget(name=target.name, host=target.host, secret_ref=path),
        operator,
        param=f"inputs.{item.name}.secret_ref",
        mount=DEFAULT_KV_MOUNT,
    )
    raw = data.get(field) if isinstance(data, dict) else None
    value = strip_credential_value(raw) if isinstance(raw, str) else ""
    if not value:
        raise VroRunRefusedError(
            f"input {item.name!r}: the Vault secret at {path!r} has no text under the key "
            f"{field!r}. Nothing was sent."
        )
    return value


def _answer(
    status: str,
    workflow: dict[str, Any],
    digest: str,
    message: str,
    *,
    execution_id: Any = None,
    state: Any = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "workflow_id": workflow.get("id"),
        "workflow_name": workflow.get("name"),
        "execution_id": execution_id,
        "state": state,
        "run_sha256": digest,
        "status_read": STATUS_READ_OP_ID,
        "message": message,
    }


def _outcome(state: Any) -> str | None:
    return OUTCOME_BY_STATE.get(state.strip().lower()) if isinstance(state, str) else None


async def _running_execution(
    connector: VroConnector, operator: Operator, target: VroTargetLike, workflow_id: str
) -> dict[str, Any] | None:
    """A run of the workflow that is still running or waiting, if the list shows one."""
    payload = await connector._vro_get(
        target,
        _EXECUTIONS_PATH.format(workflowId=path_segment("workflow", workflow_id)),
        operator=operator,
        params={"maxResult": _RUNNING_CHECK_ROWS, "startIndex": 0},
    )
    relations = payload.get("relations")
    links = relations.get("link") if isinstance(relations, dict) else None
    for row in flat_rows(links):
        if _outcome(row.get("state")) in _ACTIVE_OUTCOMES:
            return row
    return None


def _mentions(text: str, secrets: list[str]) -> bool:
    for secret in secrets:
        forms = {secret, json.dumps(secret)[1:-1], quote(secret, safe=""), html.escape(secret)}
        if any(form and form in text for form in forms):
            return True
    return False


def _safe_error(exc: httpx.HTTPStatusError, secrets: list[str]) -> Exception:
    """The error the run raises for a vendor error answer, with no request body in it.

    A ``401`` stays an :class:`httpx.HTTPStatusError` (rebuilt from the method
    and URL alone), so the dispatcher logs in again and retries once. Any
    other status is a :class:`ConnectorUpstreamError` with the vendor text,
    or without it when the text holds a secret input value.
    """
    status = exc.response.status_code
    request = httpx.Request(exc.request.method, exc.request.url)
    if status == httpx.codes.UNAUTHORIZED:
        return httpx.HTTPStatusError(
            f"Orchestrator answered HTTP {status}",
            request=request,
            response=httpx.Response(status, request=request),
        )
    try:
        text = exc.response.text
    except (UnicodeDecodeError, httpx.StreamError):
        text = ""
    upstream = _WITHHELD if _mentions(text, secrets) else (text[:_VENDOR_TEXT_MAX] or None)
    return ConnectorUpstreamError(
        f"Orchestrator refused to start the workflow run (HTTP {status}). Nothing runs.",
        http_status=status,
        upstream_message=upstream,
    )


def _execution_id(response: httpx.Response, body: dict[str, Any]) -> str | None:
    """The run id from the answer body, or from the end of the ``Location`` header."""
    run_id = body.get("id")
    if isinstance(run_id, str) and run_id:
        return run_id
    location = response.headers.get("location")
    if not location:
        return None
    tail = httpx.URL(location).path.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def _card_problem(seen: dict[str, Any] | None, digest: str) -> str | None:
    """Why the approved run must not start, or ``None`` when the card matches."""
    if seen is None:
        return (
            "This run has no approval card bound. A workflow runs only after a person "
            "approved its preview. Nothing was started."
        )
    if seen.get("run_sha256") != digest:
        return (
            "The workflow or the inputs changed since the approval: run_sha256 differs "
            "from the approved card. Nothing was started. Preview again and ask for a "
            "new approval."
        )
    return None


async def vro_workflow_run_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.workflow.run`` -- start an approved workflow run; see the module doc."""
    workflow, inputs, digest = await _read_and_check(connector, operator, target, params)
    problem = _card_problem(approved_preview(RUN_OP_ID), digest)
    if problem is not None:
        return _answer("precondition_failed", workflow, digest, problem)
    workflow_id = str(workflow.get("id") or params["workflow_id"])
    if params.get("allow_parallel") is not True:
        running = await _running_execution(connector, operator, target, workflow_id)
        if running is not None:
            return _answer(
                "already_running",
                workflow,
                digest,
                "A run of this workflow is still running or waiting, so no new run was "
                "started. Read it with vro.execution.get, or set allow_parallel.",
                execution_id=running.get("id"),
                state=running.get("state"),
            )
    secrets: list[str] = []
    parameters: list[dict[str, Any]] = []
    for item in inputs:
        value = item.value
        if item.secret_ref is not None:
            value = await _read_secret(target, operator, item)
            secrets.append(value)
        parameters.append(
            {
                "name": item.name,
                "type": item.type,
                "scope": "local",
                "value": wrap_value(item.type, value, name=item.name),
            }
        )
    path = _EXECUTIONS_PATH.format(workflowId=path_segment("workflow", workflow_id))
    error: Exception | None = None
    try:
        response = await connector._vro_send(
            target, "POST", path, operator=operator, json={"parameters": parameters}
        )
    except httpx.HTTPStatusError as exc:
        error = _safe_error(exc, secrets)
    if error is not None:
        raise error
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    run_id = _execution_id(response, body)
    return _answer(
        "accepted",
        workflow,
        digest,
        "Orchestrator accepted the run. It is not done yet: read its state with "
        "vro.execution.get until it ends.",
        execution_id=run_id,
        state=body.get("state"),
    )


async def vro_execution_cancel_impl(
    connector: VroConnector,
    operator: Operator,
    target: VroTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vro.execution.cancel`` -- cancel a run that has not ended; see the module doc."""
    workflow_id = path_segment("workflow", params.get("workflow_id"))
    execution_id = path_segment("execution", params.get("execution_id"))
    path = _STATE_PATH.format(workflowId=workflow_id, executionId=execution_id)
    state_payload = await connector._vro_get(target, path, operator=operator)
    state = state_payload.get("value")
    answer = {
        "workflow_id": params.get("workflow_id"),
        "execution_id": params.get("execution_id"),
        "previous_state": state,
        "status_read": STATUS_READ_OP_ID,
    }
    if _outcome(state) in _ENDED_OUTCOMES:
        return {
            **answer,
            "status": "unchanged",
            "message": "The run already ended; nothing was sent.",
        }
    await connector._vro_send(target, "DELETE", path, operator=operator)
    return {
        **answer,
        "status": "cancel_requested",
        "message": (
            "Orchestrator accepted the cancel. Read vro.execution.get until the state is "
            "canceled. Steps that already ran are not undone."
        ),
    }


register_preview_builder(RUN_OP_ID, _workflow_run_preview)
