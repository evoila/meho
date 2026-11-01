# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Publish a local content library, or stop publishing it.

``vmware.composite.content_library.publish.set`` (``caution``, waits for
approval) turns publishing of a LOCAL library on or off and sets how
subscribers log in (``PATCH /content/local-library/{id}``), then reads the
library back. Settings that are already so give ``unchanged``.

Passwords come only as Vault references (``password_ref``,
``current_password_ref``). MEHO reads them under the caller's identity just
after the shared write check (:func:`._library_capture.check_step`) passes,
just before the call. The step check and an approval request name the
references, never the values, and no answer holds a password: a failed
change keeps only the HTTP status and vCenter's error type, not its error
text. The operation's ``password`` tag keeps the vendor request bodies out
of the flight recorder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.vmware_rest.composites._library_capture import (
    check_step,
    failed_answer,
    local_library,
    send_step,
)
from meho_backplane.connectors.vmware_rest.composites._library_checks import (
    OP_GET_LIBRARY,
    get_or_none,
)
from meho_backplane.connectors.vmware_rest.composites._library_export_schemas import (
    PUBLISH_SET_LLM_INSTRUCTIONS,
    PUBLISH_SET_PARAMETER_SCHEMA,
    PUBLISH_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_FIND_LIBRARY,
    _parse_vsphere_error,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    preview_failure_reason,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = ["PUBLISH_SET_COMPOSITE", "content_library_publish_set_composite"]

OP_ID_PUBLISH_SET: Final = "vmware.composite.content_library.publish.set"
OP_GET_LOCAL_LIBRARY: Final = "GET:/content/local-library/{libraryId}"
OP_UPDATE_LOCAL_LIBRARY: Final = "PATCH:/content/local-library/{libraryId}"

#: Every REST call of the change (reads and the write). The reconcile test
#: checks them against the pinned vCenter 8.0 and 9.0 API files, and
#: ``_governed_subops`` lists them for the grant set.
_SUB_OPS_CONTENT_LIBRARY_PUBLISH_SET: Final[tuple[str, ...]] = (
    _OP_FIND_LIBRARY,
    OP_GET_LIBRARY,
    OP_GET_LOCAL_LIBRARY,
    OP_UPDATE_LOCAL_LIBRARY,
)

_BASIC: Final = "BASIC"
_NONE: Final = "NONE"
_PASSWORD_KEY: Final = "password"


@dataclass
class _SecretRef:
    """The shape the Vault reader wants: a target name, a host and the reference."""

    name: str
    host: str
    secret_ref: str | None


async def _read_password(target: Any, operator: Operator, ref: str, param: str) -> str:
    secret = _SecretRef(
        name=str(getattr(target, "name", "")),
        host=str(getattr(target, "host", "")),
        secret_ref=ref,
    )
    data = await load_param_secret_data(secret, operator, param=param)
    value = data.get(_PASSWORD_KEY)
    password = strip_credential_value(value) if isinstance(value, str) else ""
    if not password:
        raise ValueError(f"the secret named by {param} has no value under {_PASSWORD_KEY!r}")
    return password


def _publish_row(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict):
        return None
    return {
        "published": info.get("published"),
        "publish_url": info.get("publish_url"),
        "authentication_method": info.get("authentication_method"),
        "user_name": info.get("user_name"),
    }


@dataclass(frozen=True)
class _PublishPlan:
    """What the change sends; the refs, never the passwords."""

    published: bool
    authentication: str | None
    password_ref: str | None
    current_password_ref: str | None

    def info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"published": self.published}
        if self.authentication is not None:
            info["authentication_method"] = self.authentication
        return info

    def check_params(self, library_id: str) -> dict[str, Any]:
        shown = self.info()
        if self.password_ref is not None:
            shown["password_ref"] = self.password_ref
        if self.current_password_ref is not None:
            shown["current_password_ref"] = self.current_password_ref
        return {"libraryId": library_id, "publish_info": shown}


def _publish_problem(params: dict[str, Any], before: dict[str, Any]) -> tuple[str, str] | None:
    """``(status, guidance)`` when the change cannot run as asked, else ``None``."""
    current = before.get("authentication_method")
    wanted = params.get("authentication") or current
    if wanted == _BASIC and current != _BASIC and not params.get("password_ref"):
        return "invalid_request", "BASIC needs password_ref (the Vault path of the password)"
    if wanted == _NONE and params.get("password_ref"):
        return "invalid_request", "password_ref is only for BASIC"
    needs_current = current == _BASIC and (
        bool(params.get("password_ref"))
        or wanted != _BASIC
        or (before.get("published") and not params["published"])
    )
    if needs_current and not params.get("current_password_ref"):
        return (
            "precondition_failed",
            "The library uses BASIC; vCenter needs the current password for this change. "
            "Pass current_password_ref. Nothing was changed.",
        )
    return None


def _publish_unchanged(params: dict[str, Any], before: dict[str, Any]) -> bool:
    auth = params.get("authentication")
    return (
        bool(before.get("published")) == params["published"]
        and (auth is None or auth == before.get("authentication_method"))
        and not params.get("password_ref")
    )


async def _publish_plan(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
    answer: dict[str, Any],
) -> tuple[_PublishPlan | None, dict[str, Any] | None]:
    """Read-only checks: ``(plan, None)``, or ``(None, answer)`` when the call stops."""
    library_id, refusal = await local_library(connector, target, operator, params)
    if refusal is not None or library_id is None:
        return None, {**answer, **(refusal or {})}
    answer["library_id"] = library_id
    local = await get_or_none(
        connector, target, operator, OP_GET_LOCAL_LIBRARY, {"libraryId": library_id}
    )
    before = _publish_row((local or {}).get("publish_info")) or {}
    answer["before"] = before or None
    problem = _publish_problem(params, before)
    if problem is not None:
        return None, {**answer, "status": problem[0], "guidance": problem[1]}
    if _publish_unchanged(params, before):
        return None, {
            **answer,
            "status": "unchanged",
            "publish": before or None,
            "guidance": "The library already has these settings; nothing was changed.",
        }
    plan = _PublishPlan(
        published=bool(params["published"]),
        authentication=params.get("authentication"),
        password_ref=params.get("password_ref"),
        current_password_ref=params.get("current_password_ref"),
    )
    return plan, None


async def content_library_publish_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Publish or unpublish a local library (``content_library.publish.set``)."""
    answer: dict[str, Any] = {
        "library_id": None,
        "before": None,
        "publish": None,
        "password_changed": False,
        "error_type": None,
        "messages": [],
        "guidance": None,
    }
    plan, stop = await _publish_plan(connector, target, operator, params, answer)
    if stop is not None or plan is None:
        return stop or answer
    library_id = str(answer["library_id"])
    gate = await check_step(
        connector,
        target,
        operator,
        OP_UPDATE_LOCAL_LIBRARY,
        params=plan.check_params(library_id),
    )
    if gate is not None:
        return gate
    # The passwords are read only now, after the check passed, and leave the
    # body dict when the call ends.
    info = plan.info()
    try:
        if plan.password_ref is not None:
            info["password"] = await _read_password(
                target, operator, plan.password_ref, "password_ref"
            )
        if plan.current_password_ref is not None:
            info["current_password"] = await _read_password(
                target, operator, plan.current_password_ref, "current_password_ref"
            )
        await send_step(
            connector,
            target,
            operator,
            OP_UPDATE_LOCAL_LIBRARY,
            path_params={"libraryId": library_id},
            body={"publish_info": info},
        )
    except httpx.HTTPError as exc:
        # vCenter's error text can repeat what was sent, so it is not kept.
        failed = failed_answer(answer, exc, "The change", withhold=True)
        status, _ = _parse_vsphere_error(exc)
        if status == 400:
            return {**failed, "status": "refused", "issues": []}
        return failed
    finally:
        info.pop("password", None)
        info.pop("current_password", None)
    local = await get_or_none(
        connector, target, operator, OP_GET_LOCAL_LIBRARY, {"libraryId": library_id}
    )
    return {
        **answer,
        "status": "updated",
        "publish": _publish_row((local or {}).get("publish_info")),
        "password_changed": plan.password_ref is not None,
        "guidance": "The publish settings are changed.",
    }


async def _publish_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the approver the library, its publish settings now and the change."""
    if ctx.connector_instance is None:
        return None
    connector: VmwareRestConnector = ctx.connector_instance  # type: ignore[assignment]
    params = dict(ctx.params)
    effect: dict[str, Any] = {
        "published": params.get("published"),
        "authentication": params.get("authentication"),
        "password_ref": params.get("password_ref"),
    }
    answer: dict[str, Any] = {"library_id": None, "before": None}
    try:
        _plan, stop = await _publish_plan(connector, ctx.target, ctx.operator, params, answer)
    except Exception as exc:
        return {
            **effect,
            "summary": "MEHO could not read vCenter, so it cannot check the library.",
            "read_error": preview_failure_reason(exc),
        }
    effect.update(library_id=answer.get("library_id"), before=answer.get("before"))
    if stop is not None:
        return {**effect, "summary": f"The call will change nothing: {stop.get('guidance')}"}
    state = "on" if params.get("published") else "off"
    return {
        **effect,
        "summary": f"Publishing of library {answer.get('library_id')!r} is turned {state}.",
    }


register_preview_builder(OP_ID_PUBLISH_SET, _publish_preview)

#: The registration row, as ``_CompositeSpec`` keyword arguments.
PUBLISH_SET_COMPOSITE: Final[dict[str, Any]] = {
    "op_id": OP_ID_PUBLISH_SET,
    "handler": content_library_publish_set_composite,
    "summary": "Turn publishing of a local content library on or off and set its password.",
    "description": (
        "Publishes a local content library so other vCenters can subscribe, or stops it, "
        "and sets how subscribers log in. Passwords come only as Vault references and are "
        "never shown. Reads the library back. Waits until a second person approves."
    ),
    "parameter_schema": PUBLISH_SET_PARAMETER_SCHEMA,
    "response_schema": PUBLISH_SET_RESPONSE_SCHEMA,
    "group_key": "content_library",
    "tags": ["composite", "write", "content-library", "local", "publish", "password"],
    "safety_level": "caution",
    "requires_approval": True,
    "llm_instructions": PUBLISH_SET_LLM_INSTRUCTIONS,
}
