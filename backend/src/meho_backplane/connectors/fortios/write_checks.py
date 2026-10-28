# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks every FortiGate change runs, before and after it is sent.

The FortiGate change operations (addresses, policies, routes, VPN, ...) call
these helpers. A change runs in three steps:

1. :func:`check_before_write` -- refuse when the version is not on the
   tested list, when FortiManager controls the device ("change it in
   FortiManager"; never a direct change instead) or MEHO cannot tell, when
   the live object is no longer what the approver saw (``expected_before``),
   or when the policies and groups that use it changed
   (``expected_references``).
2. :func:`send_write` -- send the change once. A lost answer (a timeout, a
   dropped connection, an HA failover) gives the outcome ``unknown`` and is
   never sent a second time.
3. :func:`verify_after_write` -- read the object back and compare it with
   what was sent, then compare the HA checksums of the members. The outcome
   is ``verified``, ``not_verified`` or ``unknown`` (the read-back or the HA
   state could not be read).

:func:`register_write_preview` registers the preview an approver sees: the
object now and after the change, the policies and groups that use it, the
VDOM, the HA state and whether FortiManager controls the device. The values
the preview shows as ``expected_before`` and ``expected_references`` are the
ones the change must carry.

Every object here has its secrets removed (see
:mod:`~meho_backplane.connectors.fortios.redaction`), so a comparison never
needs a secret and a preview never shows one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.fortios import reads
from meho_backplane.connectors.fortios._errors import FortiOSApiError
from meho_backplane.connectors.fortios.connector import (
    CENTRAL_MANAGEMENT_PATH,
    HA_CHECKSUMS_PATH,
    FortiOSConnector,
)
from meho_backplane.connectors.fortios.redaction import REDACTED, scrub, scrub_text
from meho_backplane.connectors.fortios.target import resolve_scope
from meho_backplane.connectors.fortios.versions import (
    COVERED_RANGE_TEXT,
    is_tested,
    require_covered,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = [
    "OBJECT_USAGE_PATH",
    "BeforeWrite",
    "CmdbObject",
    "FortiOSWriteRefusedError",
    "Outcome",
    "SendResult",
    "Verification",
    "WritePreviewSpec",
    "build_write_preview",
    "check_before_write",
    "read_object",
    "read_references",
    "register_write_preview",
    "send_write",
    "verify_after_write",
]

#: Lists the configuration entries that use an object.
OBJECT_USAGE_PATH: Final[str] = "monitor/system/object/usage"

Outcome = Literal["verified", "not_verified", "unknown"]
Sleep = Callable[[float], Awaitable[None]]

#: How often, and how far apart, the HA checksums are compared after a change.
#: A change takes a moment to reach the other member.
HA_ATTEMPTS: Final[int] = 5
HA_DELAY_SECONDS: Final[float] = 2.0


class FortiOSWriteRefusedError(RuntimeError):
    """A check before the write failed, so nothing was sent. ``code`` says which."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CmdbObject:
    """One configuration object: its table (``firewall/address``) and key, if any."""

    path: str
    mkey: str | None = None

    @property
    def api_path(self) -> str:
        base = f"cmdb/{self.path}"
        return base if self.mkey is None else f"{base}/{quote(self.mkey, safe='')}"

    def usage_query(self) -> dict[str, str]:
        """``q_path`` / ``q_name`` / ``mkey`` of the object usage call."""
        q_path, _, q_name = self.path.rpartition("/")
        return {"q_path": q_path, "q_name": q_name, "mkey": self.mkey or ""}


@dataclass(frozen=True)
class BeforeWrite:
    """What :func:`check_before_write` found: all checks passed."""

    version: str
    live: dict[str, Any] | None
    references: tuple[str, ...]


@dataclass(frozen=True)
class SendResult:
    """The answer to a sent change, or ``unknown`` when the answer was lost."""

    outcome: Literal["answered", "unknown"]
    results: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class Verification:
    """The result of :func:`verify_after_write`."""

    outcome: Outcome
    reasons: tuple[str, ...] = ()
    read_back: dict[str, Any] | None = None
    ha_in_sync: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "read_back": self.read_back,
            "ha_in_sync": self.ha_in_sync,
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _first_row(results: Any) -> dict[str, Any] | None:
    if isinstance(results, list):
        return dict(results[0]) if results and isinstance(results[0], Mapping) else None
    return dict(results) if isinstance(results, Mapping) else None


async def read_object(
    connector: FortiOSConnector, operator: Operator, target: Any, obj: CmdbObject
) -> dict[str, Any] | None:
    """The object with its secrets removed, or ``None`` when it does not exist (404)."""
    try:
        answer = await connector.api_get(target, operator, obj.api_path)
    except FortiOSApiError as exc:
        if exc.http_status == 404:
            return None
        raise
    return _first_row(answer.results)


async def read_references(
    connector: FortiOSConnector, operator: Operator, target: Any, obj: CmdbObject
) -> tuple[str, ...]:
    """The entries that use *obj*, as sorted ``<table>:<key>`` texts.

    Raises :class:`FortiOSWriteRefusedError` when the answer has no list MEHO can
    read, so a change never goes ahead on an unknown list of users.
    """
    answer = await connector.api_get(target, operator, OBJECT_USAGE_PATH, query=obj.usage_query())
    results = answer.results if isinstance(answer.results, Mapping) else {}
    using = results.get("currently_using")
    if not isinstance(using, list):
        raise FortiOSWriteRefusedError(
            f"MEHO could not read which entries use {obj.api_path}, so the change was not sent",
            code="references_unknown",
        )
    refs = {
        f"{row.get('path')}/{row.get('name')}:{row.get('mkey')}"
        for row in using
        if isinstance(row, Mapping)
    }
    return tuple(sorted(refs))


async def fortimanager_controls(
    connector: FortiOSConnector, operator: Operator, target: Any
) -> tuple[bool | None, str | None]:
    """Whether FortiManager controls the device (``None``: unknown), and the answer's version."""
    answer = await connector.api_get(target, operator, CENTRAL_MANAGEMENT_PATH)
    return reads.fortimanager_state(answer.results)["fortimanager_controls"], answer.version


async def check_before_write(
    connector: FortiOSConnector,
    operator: Operator,
    target: Any,
    *,
    obj: CmdbObject,
    expected_before: Mapping[str, Any] | None,
    expected_references: Sequence[str] | None,
) -> BeforeWrite:
    """Run every check before a change; raise :class:`FortiOSWriteRefusedError` on a failure.

    ``expected_before`` is the object as the approver saw it in the preview,
    or ``None`` for a create (the object must not exist yet).
    ``expected_references`` is the list of users the approver saw. A create
    has no users, so it skips that check and makes no usage call. A change or
    delete must carry the list: ``None`` means the approver did not see it, so
    the change is refused (``references_unknown``).
    """
    controls, version_text = await fortimanager_controls(connector, operator, target)
    version = require_covered(version_text)
    if not is_tested(version):
        raise FortiOSWriteRefusedError(
            f"FortiOS {'.'.join(map(str, version))} is covered ({COVERED_RANGE_TEXT}) but not "
            "on the tested list, so MEHO refuses changes on it",
            code="version_untested",
        )
    if controls is None:
        raise FortiOSWriteRefusedError(
            "MEHO could not tell whether FortiManager controls this FortiGate (the central "
            "management answer has no mode or server MEHO knows), so the change was not sent",
            code="fortimanager_unknown",
        )
    if controls:
        raise FortiOSWriteRefusedError(
            "FortiManager controls this FortiGate: change it in FortiManager. MEHO does not "
            "change the device directly",
            code="fortimanager_controls",
        )
    live = await read_object(connector, operator, target, obj)
    expected = scrub(dict(expected_before))[0] if expected_before is not None else None
    if _canonical(live) != _canonical(expected):
        raise FortiOSWriteRefusedError(
            f"{obj.api_path} is not what the approver saw (it changed, appeared or was "
            "deleted since the preview). Preview the change again",
            code="object_changed",
        )
    references: tuple[str, ...] = ()
    if expected_before is not None:
        if expected_references is None:
            raise FortiOSWriteRefusedError(
                f"the preview did not show which entries use {obj.api_path}, so the change "
                "was not sent. Preview the change again",
                code="references_unknown",
            )
        references = await read_references(connector, operator, target, obj)
        if references != tuple(sorted(expected_references)):
            raise FortiOSWriteRefusedError(
                f"the policies and groups that use {obj.api_path} changed since the preview. "
                "Preview the change again",
                code="references_changed",
            )
    return BeforeWrite(version=".".join(map(str, version)), live=live, references=references)


async def send_write(
    connector: FortiOSConnector,
    operator: Operator,
    target: Any,
    *,
    method: Literal["POST", "PUT", "DELETE"],
    obj: CmdbObject,
    body: dict[str, Any] | None = None,
) -> SendResult:
    """Send the change once. A lost answer gives ``unknown``, never a second call.

    A connection that never opened (``ConnectError``) sent nothing and is
    raised as it is. An error answer from FortiOS is raised as
    :class:`~meho_backplane.connectors.fortios._errors.FortiOSApiError`.
    """
    try:
        answer = await connector.api_write(target, operator, method, obj.api_path, body=body)
    except httpx.ConnectError:
        raise
    except httpx.TransportError as exc:
        return SendResult(
            outcome="unknown",
            reason=(
                f"the answer to {method} {obj.api_path} was lost ({type(exc).__name__}); the "
                "change may or may not be applied. MEHO does not send it again. Read the "
                "object to see its state"
            ),
        )
    return SendResult(outcome="answered", results=answer.results)


def _differences(sent: Mapping[str, Any], read_back: Mapping[str, Any]) -> list[str]:
    """The keys of *sent* whose read-back value differs (secret values are not compared)."""
    cleaned, _ = scrub(dict(sent))
    return sorted(
        key
        for key, value in cleaned.items()
        if value != REDACTED and _canonical(read_back.get(key)) != _canonical(value)
    )


def _incomplete_checksums(results: Any, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Why the HA checksum answer confirms nothing, or ``None`` when it is complete.

    A complete answer is a list with at least one member row, and every
    row names its serial and holds checksums. ``results: null``, ``{}`` or
    ``[]`` is not a unit that stands alone: it says nothing about the HA
    state.
    """
    if not isinstance(results, list):
        return f"results is not a list of members but {type(results).__name__}"
    if not rows:
        return "the list of members is empty"
    if any(not row["serial"] or not row["checksums"] for row in rows):
        return "a member row has no serial or no checksums"
    return None


async def _ha_in_sync(
    connector: FortiOSConnector, operator: Operator, target: Any, sleep: Sleep
) -> tuple[bool | None, str | None]:
    """Compare the HA checksums, a few times: ``(in_sync, failure)``.

    ``in_sync`` is ``None`` for one unit (one complete checksum row).
    ``failure`` says why the HA state could not be read (a 403, a timeout,
    a FortiOS error) or why the answer confirms nothing (no member rows, a
    row without serial or checksums); then ``in_sync`` is ``None`` too and
    the caller does not know whether the members agree.
    """
    in_sync: bool | None = None
    for attempt in range(HA_ATTEMPTS):
        try:
            answer = await connector.api_get(target, operator, HA_CHECKSUMS_PATH)
        except (httpx.TransportError, FortiOSApiError, ConnectorAuthError) as exc:
            return None, scrub_text(f"the HA state could not be read: {exc}")
        rows = reads.checksums(answer.results)
        incomplete = _incomplete_checksums(answer.results, rows)
        if incomplete is not None:
            return None, f"the HA state is incomplete: {incomplete}"
        in_sync = reads.ha_in_sync(rows)
        if in_sync is not False:
            return in_sync, None
        if attempt + 1 < HA_ATTEMPTS:
            await sleep(HA_DELAY_SECONDS)
    return in_sync, None


async def verify_after_write(
    connector: FortiOSConnector,
    operator: Operator,
    target: Any,
    *,
    obj: CmdbObject,
    sent: Mapping[str, Any] | None,
    sleep: Sleep = asyncio.sleep,
) -> Verification:
    """Read the object back and compare the HA checksums.

    *sent* is the body of a create or change, or ``None`` for a delete (the
    object must be gone). ``verified``: the read-back matches and the HA
    members agree (or there is one unit). ``not_verified``: the read-back
    differs, or the members still differ after a few tries. ``unknown``: the
    read-back failed, or the read-back matches but the HA state could not be
    read or the answer confirms nothing, so MEHO does not know whether every
    member has the change.
    """
    try:
        read_back = await read_object(connector, operator, target, obj)
    except (httpx.TransportError, FortiOSApiError, ConnectorAuthError) as exc:
        return Verification(outcome="unknown", reasons=(scrub_text(f"read-back failed: {exc}"),))
    reasons: list[str] = []
    if sent is None and read_back is not None:
        reasons.append(f"{obj.api_path} still exists after the delete")
    if sent is not None:
        if read_back is None:
            reasons.append(f"{obj.api_path} does not exist after the change")
        else:
            diff = _differences(sent, read_back)
            if diff:
                reasons.append(f"read-back differs in: {', '.join(diff)}")
    in_sync, ha_failure = await _ha_in_sync(connector, operator, target, sleep)
    if in_sync is False:
        reasons.append("the HA members do not have the same configuration yet")
    outcome: Outcome = "not_verified" if reasons else "verified"
    if ha_failure is not None:
        reasons.append(ha_failure)
        if outcome == "verified":
            outcome = "unknown"
    return Verification(
        outcome=outcome, reasons=tuple(reasons), read_back=read_back, ha_in_sync=in_sync
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WritePreviewSpec:
    """How to read a change operation's parameters for its preview.

    ``mkey_param`` names the parameter with the object key; ``body_param``
    the parameter with the object fields (``None`` for a delete).
    """

    action: Literal["create", "change", "delete"]
    path: str
    mkey_param: str
    body_param: str | None = None


async def _optional(call: Awaitable[Any]) -> Any:
    try:
        return await call
    except (FortiOSApiError, ConnectorAuthError, FortiOSWriteRefusedError):
        return None


async def build_write_preview(
    connector: FortiOSConnector,
    operator: Operator,
    target: Any,
    spec: WritePreviewSpec,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """The preview of one change: the object now and after, its users, VDOM, HA, FortiManager."""
    scope = resolve_scope(target)
    obj = CmdbObject(spec.path, str(params[spec.mkey_param]))
    now = await read_object(connector, operator, target, obj)
    body = params.get(spec.body_param) if spec.body_param else None
    after: dict[str, Any] | None = None
    if spec.action != "delete":
        after = {**(now or {}), **scrub(dict(body or {}))[0]}
    references = await _optional(read_references(connector, operator, target, obj)) if now else ()
    controls = await _optional(fortimanager_controls(connector, operator, target))
    ha = await _optional(connector.api_get(target, operator, HA_CHECKSUMS_PATH))
    return {
        "action": spec.action,
        "object": obj.api_path,
        "vdom": scope.vdom,
        "target_scope": scope.kind,
        "now": now,
        "after": after,
        "references": list(references) if references is not None else None,
        "ha_in_sync": reads.ha_in_sync(reads.checksums(ha.results)) if ha else None,
        "fortimanager_controls": controls[0] if controls else None,
        "expected_before": now,
        "expected_references": list(references) if references is not None else None,
    }


def register_write_preview(op_id: str, spec: WritePreviewSpec) -> None:
    """Register :func:`build_write_preview` as the preview of the change operation *op_id*."""

    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        connector = ctx.connector_instance
        if not isinstance(connector, FortiOSConnector):
            return None
        return await build_write_preview(connector, ctx.operator, ctx.target, spec, ctx.params)

    register_preview_builder(op_id, _builder)
