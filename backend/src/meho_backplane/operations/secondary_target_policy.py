# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Policy for a write an operation makes on a second, caller-named target (#3898).

Some operations write to a second target the caller names in the params.
``vcfa.provider.api_token.create`` is the first: it writes the minted
token through ``vault.kv.patch`` / ``vault.kv.put`` on the caller-named
``vault_target``. The handler calls those Vault handlers directly, so the
dispatcher's policy gate never sees that write. This module is the one
place that checks MEHO policy for it.

Who is checked
==============

* **The caller that runs the operation**
  (:func:`secondary_target_refusal`). ``deny`` refuses. ``needs-approval``
  is satisfied only by this operation's own approval.
* **The requester, when a reviewer runs an approved request.** The
  operator approval surfaces (REST ``/approve`` and ``/decide``, the MCP
  by-id approve) run the approved call *as the reviewer*. Without a second
  check, an agent's ``deny`` on the second target would never be looked
  at. So the resume binds the requester from the approval row
  (:func:`bind_approved_requester`), and :func:`secondary_target_refusal`
  checks the requester too. The rest of what the policy gate needs about
  the requester (kind, role, client id) comes from the record the park
  stores on the approval card (:func:`requester_policy_preflight`, in
  ``permission_preflight``). That record is used only when its ``sub``
  matches the row. When it is missing or does not match, the run is
  refused and the requester must ask again. Only the requester's ``deny``
  refuses otherwise: the approval satisfies its ``needs-approval``.

The execution still runs as the reviewer. The requester's policy on the
second target is still enforced, and checked again at run time, so a deny
added while the request waited still stops it.

An operation opts in by calling :func:`requester_policy_preflight` from its
park-time permission preflight and :func:`secondary_target_refusal` from its
handler, both with the same :class:`SecondaryTargetWrite`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from meho_backplane.auth.delegation import ACTOR_SUB_KEY, resolve_actor_sub
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "SecondaryTargetWrite",
    "bind_approved_requester",
    "requester_policy_preflight",
    "secondary_target_refusal",
]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SecondaryTargetWrite:
    """The ops an operation runs on its second target, and how to name that target."""

    connector_id: str
    op_ids: tuple[str, ...]
    #: How a refusal names the target, e.g. ``"Vault target"``.
    target_label: str


class _Requester(NamedTuple):
    """The requester of an approved request.

    ``sub`` and ``act`` come from the approval row. ``subject`` is the card's
    ``permission_preflight["requester"]`` record, or ``None`` when the card
    has none or its ``sub`` does not match the row.
    """

    tenant_id: Any
    sub: str
    act: str | None
    subject: Mapping[str, Any] | None


#: The requester of the approved request a reviewer is running. Bound by
#: :func:`bind_approved_requester` on the operator resume path only. ``None``
#: everywhere else, including the agent's own resume, where the caller is the
#: requester.
_approved_requester_var: ContextVar[_Requester | None] = ContextVar(
    "approved_requester", default=None
)


async def _refusal(
    operator: Operator,
    write: SecondaryTargetWrite,
    target: Any,
    *,
    approved: bool,
    decide_only: bool = False,
) -> str | None:
    """The first op in *write* that *operator* may not run on *target*; ``None`` = allowed."""
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id
    from meho_backplane.operations._validate import policy_gate

    product, version, impl_id = parse_connector_id(write.connector_id)
    for op_id in write.op_ids:
        descriptor = await lookup_descriptor(
            tenant_id=operator.tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
        )
        where = f"{op_id} on {write.target_label} {target.name!r}"
        if descriptor is None:
            return f"policy_denied: {where}: op not registered, its policy cannot be evaluated"
        verdict, reason = await policy_gate(
            operator=operator,
            descriptor=descriptor,
            target=target,
            connector_id=write.connector_id,
            decide_only=decide_only,
        )
        if verdict is PermissionVerdict.AUTO_EXECUTE:
            continue
        if verdict is PermissionVerdict.NEEDS_APPROVAL and approved:
            continue  # satisfied by this operation's own approval
        if verdict is PermissionVerdict.NEEDS_APPROVAL:
            return (
                f"policy_denied: {where} needs approval ({reason}), but this dispatch was "
                f"not approved -- dispatch it so it parks for approval, or grant that write "
                f"on the {write.target_label}"
            )
        return f"policy_denied: {where}: {reason or 'denied'}"
    return None


def _subject_of(operator: Operator) -> dict[str, Any]:
    """The fields the policy gate reads about a caller, as stored on the card."""
    return {
        "sub": operator.sub,
        "principal_kind": operator.principal_kind.value,
        "client_id": operator.client_id,
        "tenant_role": operator.tenant_role.value,
        "act": resolve_actor_sub(),
    }


async def requester_policy_preflight(
    operator: Operator, write: SecondaryTargetWrite, target: Any
) -> dict[str, Any]:
    """Park-time verdict of the requester's policy for *write* on *target*.

    Runs as the requester (the park runs on the requester's call). Returns
    the keys an operation merges into its ``permission_preflight``:
    ``requester`` (who asked, so a reviewer's resume can check them again),
    ``requester_denied`` and ``requester_reason``. ``needs-approval`` counts
    as allowed: the approval being asked for satisfies it. This never
    refuses the park; the reviewer sees the verdict and the resume enforces
    it.
    """
    reason = await _refusal(operator, write, target, approved=True, decide_only=True)
    return {
        "requester": _subject_of(operator),
        "requester_denied": reason is not None,
        "requester_reason": reason,
    }


@contextmanager
def bind_approved_requester(request: ApprovalRequest) -> Iterator[None]:
    """Bind *request*'s requester while a reviewer runs it.

    Who asked comes from the row (``principal_sub`` / ``principal_act``).
    The card's ``proposed_effect["permission_preflight"]["requester"]``
    record is kept only when its ``sub`` matches the row. Only operations
    that call :func:`secondary_target_refusal` read the binding.
    """
    preflight = (request.proposed_effect or {}).get("permission_preflight")
    subject = preflight.get("requester") if isinstance(preflight, dict) else None
    if not isinstance(subject, Mapping) or subject.get("sub") != request.principal_sub:
        subject = None
    requester = _Requester(request.tenant_id, request.principal_sub, request.principal_act, subject)
    token = _approved_requester_var.set(requester)
    try:
        yield
    finally:
        _approved_requester_var.reset(token)


def _requester_operator(requester: _Requester, subject: Mapping[str, Any]) -> Operator:
    """Rebuild the requester as an :class:`Operator` for the policy gate."""
    return Operator(
        sub=requester.sub,
        raw_jwt="",
        tenant_id=requester.tenant_id,
        tenant_role=TenantRole(subject["tenant_role"]),
        principal_kind=PrincipalKind(subject["principal_kind"]),
        client_id=subject.get("client_id"),
    )


async def _approved_requester_refusal(
    operator: Operator, write: SecondaryTargetWrite, target: Any
) -> str | None:
    """Check the bound requester of the approved request; ``None`` = allowed or none bound."""
    requester = _approved_requester_var.get()
    if requester is None or requester.sub == operator.sub:
        return None  # not a reviewer's run, or the requester runs it (checked as the caller)
    cannot_check = (
        f"policy_denied: the requester's policy for the write on {write.target_label} "
        f"{target.name!r} cannot be checked"
    )
    if requester.subject is None:
        return (
            f"{cannot_check} (the approval card has no requester record for "
            f"{requester.sub!r}) -- ask again so a new request records it"
        )
    try:
        principal = _requester_operator(requester, requester.subject)
    except (KeyError, TypeError, ValueError):
        return f"{cannot_check} (the approval card's requester record is unreadable)"
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: requester.act}):
        reason = await _refusal(principal, write, target, approved=True, decide_only=True)
    if reason is None:
        return None
    _log.info(
        "secondary_target_requester_denied",
        requester_sub=principal.sub,
        operator_sub=operator.sub,
        target_name=target.name,
    )
    return f"policy_denied: requester {principal.sub!r}: {reason.removeprefix('policy_denied: ')}"


async def secondary_target_refusal(
    operator: Operator, write: SecondaryTargetWrite, target: Any
) -> str | None:
    """Check MEHO policy for *write* on *target*, before any appliance call; ``None`` = allowed.

    Checks the caller first:

    * ``deny`` → refused (fail closed).
    * ``auto-execute`` → allowed.
    * ``needs-approval`` → allowed only on the approved re-dispatch of this
      operation (the dispatcher binds ``policy_decision_var`` to
      ``needs-approval`` there). A dispatch that ran at once -- e.g. a
      service principal whose standing grant covers the operation but not
      the write on that target -- is refused.

    Then, when a reviewer runs an approved request
    (:func:`bind_approved_requester`), checks the requester the same way,
    live: a requester ``deny`` refuses.

    Returns the refusal reason (naming the target), or ``None``.
    """
    from meho_backplane.db.models import PermissionVerdict
    from meho_backplane.operations._audit import policy_decision_var

    approved = policy_decision_var.get() == PermissionVerdict.NEEDS_APPROVAL.value
    refusal = await _refusal(operator, write, target, approved=approved)
    if refusal is not None:
        return refusal
    return await _approved_requester_refusal(operator, write, target)
