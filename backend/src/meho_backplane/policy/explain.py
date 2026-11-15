# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho policy explain``: why a call runs, waits or is denied, and who may approve it.

Nothing runs and nothing is written. The answer comes from the same code
the call uses:

* **Today's answer**: the preview's policy fields
  (:func:`~meho_backplane.operations._request_preview._governance_fields`),
  which call :func:`~meho_backplane.operations._validate.policy_gate` with
  ``decide_only=True``. No audit row, no approval request, no grant use.
* **The evaluator's answer** (only with ``POLICY_ENGINE_SHADOW`` on):
  :func:`~meho_backplane.policy.shadow.evaluate_call`, with every rule it
  matched in order, and the floor that decided.
* **The classification**: every classification entry that matches the
  operation, with its place in the policy (``tenant:classifications[2]``).
* **Who may approve**: :mod:`meho_backplane.policy.who_approves`.

Who asks:

* the caller (``me``), or
* a person, an agent or a service login with the given login groups and no
  grants (``groups`` and ``principal``). Such a login has no standing
  grant, no agent grant and no read scope, so the answer is the one of the
  shipped rules and the policy.

A policy file that is not imported (``file``) takes the place of the
stored policy of its scope, for the classification and the routing. A
stored approval request (``request_id``) answers with the rule stored on
it.

While the evaluator is off or in shadow mode, today's checks decide, so
``answer`` is today's answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, PermissionVerdict
from meho_backplane.policy.classification import (
    _SCOPES,
    ClassificationEntry,
    OpCoordinates,
    Scope,
    _scope_wait,
    classify,
    entries_from_document,
    taxonomy_from_documents,
)
from meho_backplane.policy.evaluate import Decision, RuleRef, Verdict
from meho_backplane.policy.explain_asker import ExplainInputError, asker_view
from meho_backplane.policy.explain_models import (
    Answer,
    ExplainApprovers,
    ExplainClassification,
    ExplainEntry,
    ExplainEvaluator,
    ExplainRule,
    ExplainStoredRequest,
    ExplainToday,
    PolicyExplainResponse,
)
from meho_backplane.policy.routing import RouteEntry, _routes, routing_enabled, stored_kinds
from meho_backplane.policy.routing import today_kinds as _today_kinds
from meho_backplane.policy.shadow import evaluate_call, shadow_enabled
from meho_backplane.policy.stamp import forget_rule
from meho_backplane.policy.store import (
    _documents,
    _group_keys,
    _override_entries,
    _stored_entries,
    classification_enabled,
    connector_id_of,
)
from meho_backplane.policy.who_approves import (
    RIGHTS_CHANGE_RULE,
    Approvers,
    approvers_for,
    approvers_from_routes,
)

__all__ = [
    "FILE_NEEDS_CLASSIFICATION",
    "CallQuestion",
    "explain_call",
    "explain_request",
]

#: The answer to ``file`` while the ``POLICY_CLASSIFICATION`` switch is off.
FILE_NEEDS_CLASSIFICATION: Final[str] = (
    "A policy file counts only while the POLICY_CLASSIFICATION switch is on. "
    "It is off, so the file would change no answer."
)

#: The label of the entries of a file that is not imported.
FILE_LABEL: Final[str] = "file"

_FROM_TODAY: Final[dict[PermissionVerdict, Answer]] = {
    PermissionVerdict.AUTO_EXECUTE: "run",
    PermissionVerdict.NEEDS_APPROVAL: "wait",
    PermissionVerdict.DENY: "deny",
}

_FLOOR_SOURCES: Final[frozenset[str]] = frozenset({"floor", "role_floor"})


@dataclass(frozen=True)
class CallQuestion:
    """One question about a call: who asks, which operation, and on which target.

    *file_document* is a checked policy file (``document_of``) that takes
    the place of the stored policy of *file_scope*.
    """

    asker: Operator
    me: bool
    connector_id: str
    op_id: str
    groups: Sequence[str] = ()
    target: str | None = None
    file_document: Mapping[str, Any] | None = field(default=None)
    file_scope: Scope = "tenant"


# ---------------------------------------------------------------------------
# The classification, from the stored policy or from a file
# ---------------------------------------------------------------------------


class _PolicyView:
    """The classification entries, the taxonomy and the routing that apply to one tenant."""

    def __init__(
        self,
        entries: list[ClassificationEntry],
        documents: tuple[Mapping[str, Any] | None, Mapping[str, Any] | None],
        routes: list[RouteEntry] | None,
    ) -> None:
        self.entries = entries
        self.taxonomy = taxonomy_from_documents(*documents)
        self.routes = routes


def _file_entries(document: Mapping[str, Any], scope: Scope) -> list[ClassificationEntry]:
    return [
        replace(entry, ref=entry.ref.replace(f"{scope}:", f"{FILE_LABEL}:", 1))
        for entry in entries_from_document(document, scope=scope)
    ]


async def _policy_view(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    file_document: Mapping[str, Any] | None,
    file_scope: Scope,
) -> _PolicyView:
    """The stored policy of the tenant and the install; *file_document* replaces one of them."""
    install, tenant = await _documents(session, tenant_id)
    install_doc = install.document if install is not None else None
    tenant_doc = tenant.document if tenant is not None else None
    labels = {"install": "install", "tenant": "tenant"}
    if file_document is None:
        install_entries = _stored_entries(install_doc, "install", "The install's stored policy")
        tenant_entries = _stored_entries(
            tenant_doc, "tenant", f"The stored policy of tenant {tenant_id}"
        )
    elif file_scope == "install":
        install_doc, labels["install"] = dict(file_document), FILE_LABEL
        install_entries = _file_entries(file_document, "install")
        tenant_entries = _stored_entries(
            tenant_doc, "tenant", f"The stored policy of tenant {tenant_id}"
        )
    else:
        tenant_doc, labels["tenant"] = dict(file_document), FILE_LABEL
        install_entries = _stored_entries(install_doc, "install", "The install's stored policy")
        tenant_entries = _file_entries(file_document, "tenant")
    routes = None
    if routing_enabled():
        routes = [*_routes(tenant_doc, labels["tenant"]), *_routes(install_doc, labels["install"])]
    return _PolicyView([*install_entries, *tenant_entries], (install_doc, tenant_doc), routes)


async def _coords(session: AsyncSession, row: EndpointDescriptor) -> OpCoordinates:
    keys = await _group_keys(session, [row], {})
    return OpCoordinates(connector_id_of(row), keys.get(row.id), row.op_id)


async def _apply_file(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    descriptor: EndpointDescriptor,
    view: _PolicyView,
    coords: OpCoordinates,
) -> list[ClassificationEntry]:
    """Set the values *descriptor* would have with the file imported; return the override entries.

    The same order as dispatch: the code's value, the overrides, the
    classification, then the floors (:func:`~meho_backplane.operations.op_policy.policy_in_force`).
    """
    from meho_backplane.operations.op_policy import (
        OpKey,
        _set_classification,
        load_overrides,
        policy_in_force,
    )

    stored = await session.get(EndpointDescriptor, descriptor.id)
    if stored is None:  # the row went away after the lookup
        raise ExplainInputError("The operation is no longer registered. Ask again.")
    overrides = (await load_overrides(session, tenant_id=tenant_id, keys=[OpKey.of(stored)])).get(
        OpKey.of(stored), []
    )
    override_entries = _override_entries(overrides, coords.connector_id)
    classified = classify(coords, [*view.entries, *override_entries], view.taxonomy)
    policy = policy_in_force(stored, overrides, classified)
    descriptor.safety_level = policy.safety_level
    descriptor.requires_approval = policy.requires_approval
    _set_classification(descriptor, policy)
    return override_entries


async def _override_entries_of(
    session: AsyncSession, tenant_id: UUID, row: EndpointDescriptor, connector_id: str
) -> list[ClassificationEntry]:
    from meho_backplane.operations.op_policy import OpKey, load_overrides

    found = await load_overrides(session, tenant_id=tenant_id, keys=[OpKey.of(row)])
    return _override_entries(found.get(OpKey.of(row), []), connector_id)


def _entry_view(entry: ClassificationEntry) -> ExplainEntry:
    return ExplainEntry(
        path=entry.ref,
        category=entry.category,
        labels=dict(entry.labels),
        risk=entry.risk,
        always_wait=entry.always_wait,
    )


def _classification_view(
    descriptor: EndpointDescriptor, matched: Sequence[ClassificationEntry]
) -> ExplainClassification:
    ordered = sorted(matched, key=lambda entry: entry.rank)
    return ExplainClassification(
        risk_level=descriptor.safety_level,
        requires_approval=bool(descriptor.requires_approval),
        category=descriptor.policy_category,
        labels=dict(descriptor.policy_labels or {}),
        entries=[_entry_view(entry) for entry in ordered],
    )


def _wait_path(matched: Sequence[ClassificationEntry]) -> str | None:
    """The entry that makes the operation always wait, as :func:`classify` decides it.

    Each scope finds its own value first, so an entry that a more specific
    entry of its scope overrode is never named. When both scopes wait, the
    more specific of the two is named.
    """
    waits = [wait for scope in _SCOPES if (wait := _scope_wait(matched, scope)) is not None]
    return max(waits, key=lambda entry: entry.rank).ref if waits else None


# ---------------------------------------------------------------------------
# The evaluator's rules
# ---------------------------------------------------------------------------


def _rule_view(rule: RuleRef, verdict: Verdict | None, wait_path: str | None) -> ExplainRule:
    path = wait_path if rule.rule_id == "always_wait" else None
    return ExplainRule(
        source=rule.source,
        rule_id=rule.rule_id,
        text=rule.text,
        verdict=verdict.value if verdict is not None else None,
        path=path,
    )


def _evaluator_view(decision: Decision, today: Answer, wait_path: str | None) -> ExplainEvaluator:
    rule = _rule_view(decision.rule, decision.verdict, wait_path)
    return ExplainEvaluator(
        verdict=decision.verdict.value,
        rule=rule,
        floor=rule if decision.rule.source in _FLOOR_SOURCES else None,
        rules=[_rule_view(step.rule, step.verdict, wait_path) for step in decision.steps],
        same_as_today=decision.verdict.value == today,
    )


# ---------------------------------------------------------------------------
# Explain one call
# ---------------------------------------------------------------------------


def _engine() -> Literal["off", "shadow"]:
    return "shadow" if shadow_enabled() else "off"


def _error(envelope: Mapping[str, Any], *, connector_id: str, op_id: str) -> PolicyExplainResponse:
    return PolicyExplainResponse(
        status="error",
        engine=_engine(),
        connector_id=str(envelope.get("connector_id") or connector_id),
        op_id=op_id,
        error=str(envelope.get("error") or "The operation could not be resolved."),
        extras=dict(envelope.get("extras") or {}),
    )


async def _classify_for_explain(
    *,
    tenant_id: UUID,
    descriptor: EndpointDescriptor,
    file_document: Mapping[str, Any] | None,
    file_scope: Scope,
) -> tuple[list[ClassificationEntry], list[RouteEntry] | None]:
    """The matching classification entries and the routing; with a file, also its values."""
    if file_document is None and not classification_enabled():
        return [], None
    async with get_sessionmaker()() as session:
        view = await _policy_view(
            session, tenant_id=tenant_id, file_document=file_document, file_scope=file_scope
        )
        coords = await _coords(session, descriptor)
        if file_document is not None:
            overrides = await _apply_file(
                session, tenant_id=tenant_id, descriptor=descriptor, view=view, coords=coords
            )
        else:
            overrides = await _override_entries_of(
                session, tenant_id, descriptor, coords.connector_id
            )
    matched = [entry for entry in [*view.entries, *overrides] if entry.matches(coords)]
    return matched, view.routes


async def _resolve(
    question: CallQuestion,
) -> tuple[EndpointDescriptor, str, Any] | PolicyExplainResponse:
    """The descriptor, connector id and target of the call, as the preview finds them."""
    from meho_backplane.operations._request_preview import _resolve_descriptor
    from meho_backplane.operations.meta_tools import _resolve_target_or_error

    target, target_error = await _resolve_target_or_error(
        question.asker, question.op_id, question.target, bind_audit_target=False
    )
    if target_error is not None:
        return _error(target_error, connector_id=question.connector_id, op_id=question.op_id)
    resolved = await _resolve_descriptor(
        operator=question.asker,
        connector_id=question.connector_id,
        op_id=question.op_id,
        target=target,
    )
    if isinstance(resolved, dict):
        return _error(resolved, connector_id=question.connector_id, op_id=question.op_id)
    descriptor, connector_id = resolved
    return descriptor, connector_id, target


async def _today_and_evaluator(
    asker: Operator, descriptor: EndpointDescriptor, connector_id: str, target: Any
) -> tuple[dict[str, Any], Decision | None]:
    """Today's policy fields and the evaluator's decision.

    Today's fields come from ``policy_gate`` with ``decide_only=True``.
    """
    from meho_backplane.auth.permissions import agent_no_grant_log_scope
    from meho_backplane.operations._request_preview import _governance_fields

    try:
        with agent_no_grant_log_scope():
            governance = await _governance_fields(
                operator=asker,
                descriptor=descriptor,
                target=target,
                connector_id=connector_id,
                params={},
            )
            decision = None
            if shadow_enabled():
                decision = await evaluate_call(
                    operator=asker,
                    descriptor=descriptor,
                    target=target,
                    connector_id=connector_id,
                    decide_only=True,
                )
    finally:
        forget_rule()  # the gate's shadow kept a rule; explain writes no row
    return governance, decision


async def _approvers(
    question: CallQuestion,
    descriptor: EndpointDescriptor,
    connector_id: str,
    routes: list[RouteEntry] | None,
) -> Approvers:
    if question.file_document is None:
        return await approvers_for(
            tenant_id=question.asker.tenant_id, connector_id=connector_id, descriptor=descriptor
        )
    return approvers_from_routes(
        routes,
        connector_id=connector_id,
        op_id=descriptor.op_id,
        level=descriptor.safety_level,
        category=descriptor.policy_category,
    )


async def explain_call(question: CallQuestion) -> PolicyExplainResponse:
    """Explain one call. Runs nothing and writes nothing."""
    if question.file_document is not None and not classification_enabled():
        raise ExplainInputError(FILE_NEEDS_CLASSIFICATION)
    resolved = await _resolve(question)
    if isinstance(resolved, PolicyExplainResponse):
        return resolved
    descriptor, connector_id, target = resolved
    matched, routes = await _classify_for_explain(
        tenant_id=question.asker.tenant_id,
        descriptor=descriptor,
        file_document=question.file_document,
        file_scope=question.file_scope,
    )
    governance, decision = await _today_and_evaluator(
        question.asker, descriptor, connector_id, target
    )
    today_verdict = PermissionVerdict(governance["policy_verdict"])
    answer = _FROM_TODAY[today_verdict]
    approvers = None
    if answer == "wait":
        approvers = _approvers_view(await _approvers(question, descriptor, connector_id, routes))
    wait_path = _wait_path(matched)
    return PolicyExplainResponse(
        status="ok",
        engine=_engine(),
        connector_id=connector_id,
        op_id=descriptor.op_id,
        target=question.target,
        file=question.file_document is not None,
        asker=asker_view(question.asker, me=question.me, groups=question.groups),
        answer=answer,
        decided_by="today",
        approvers=approvers,
        today=ExplainToday(
            verdict=answer,
            policy_verdict=today_verdict.value,
            reason=governance.get("policy_reason"),
        ),
        evaluator=_evaluator_view(decision, answer, wait_path) if decision is not None else None,
        classification=_classification_view(descriptor, matched),
    )


def _approvers_view(found: Approvers) -> ExplainApprovers:
    return ExplainApprovers(groups=list(found.groups), rule=found.rule)


# ---------------------------------------------------------------------------
# Explain a stored approval request
# ---------------------------------------------------------------------------


def _request_approvers(request: ApprovalRequest, level: str | None) -> Approvers:
    """The groups stored on *request*, or today's rule when it has none."""
    from meho_backplane.operations.grant_approval import is_rights_change

    if is_rights_change(request):
        return Approvers(("MEHO-Admins",), RIGHTS_CHANGE_RULE)
    kinds = stored_kinds(request.approver_roles)
    if kinds is not None:
        return Approvers(tuple(f"MEHO-{kind}" for kind in kinds), request.approver_rule or "")
    return Approvers(
        tuple(f"MEHO-{kind}" for kind in _today_kinds(level, request.op_id)), "default"
    )


async def explain_request(*, tenant_id: UUID, request_id: UUID) -> PolicyExplainResponse:
    """The rule stored on the approval request *request_id* of *tenant_id*.

    Raises :class:`~meho_backplane.operations.approval_queue.ApprovalNotFoundError`
    when the tenant has no such request.
    """
    from meho_backplane.operations.approval_queue import get_request

    async with get_sessionmaker()() as session:
        request = await get_request(session, tenant_id=tenant_id, request_id=request_id)
        effect = request.proposed_effect or {}
        level = effect.get("safety_level")
        level = level if isinstance(level, str) else None
        stored = ExplainStoredRequest(
            request_id=request.id,
            op_id=request.op_id,
            connector_id=request.connector_id,
            status=request.status,
            risk_level=level,
            policy_rule=dict(request.policy_rule) if request.policy_rule else None,
            policy_version=request.policy_version,
            approvers=_approvers_view(_request_approvers(request, level)),
        )
    return PolicyExplainResponse(
        status="ok",
        engine=_engine(),
        connector_id=stored.connector_id,
        op_id=stored.op_id,
        request=stored,
    )
