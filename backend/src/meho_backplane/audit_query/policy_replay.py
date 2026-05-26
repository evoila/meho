# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Policy-replay sense -- Initiative #805, Task #1074 (G11.4-T5).

The **second** audit-replay sense the Initiative ships. The first --
:func:`~meho_backplane.audit_query.replay.replay_session` (G8.2-T3
#1011) -- reconstructs *what the agent saw* by walking the agent's
session lineage and re-rendering the per-tool-call audit rows in
chronological order. This module ships the complementary sense:
*could we still produce that redacted view if we re-ran the recorded
policy against the captured raw payload today?*

The motivating question is policy regression. Audit rows carry three
parallel artefacts the connector-boundary middleware (G11.4-T2 #1071)
writes verbatim:

* ``raw_payload`` -- the connector's response before the redactor ran.
* ``redaction_manifest`` -- every rule firing the engine emitted on
  that raw payload, with ``rule`` / ``pattern`` / ``action`` /
  ``count`` / ``span`` / ``reason`` / ``path``.
* ``payload.redaction_policy_id`` -- the id of the resolved
  :class:`~meho_backplane.redaction.policy.RedactionPolicy` at the
  time the row was written.

A policy edit -- a new rule, a tightened pattern, a removed action --
should reproduce the same manifest on the same raw payload, modulo
intentional changes the author flagged in the diff. The C1-d
round-trip CI gate (#1073, parallel work) consumes this primitive at
CI time to fail builds that change a policy without bumping its
``version`` in a way the author expected.

The function is **pure with respect to the redaction engine** -- it
delegates to :func:`~meho_backplane.redaction.engine.redact` for the
replay, just as the middleware does on the hot path. Identical inputs
must produce identical outputs (the engine's deterministic contract);
a non-empty diff is therefore unambiguously a behaviour change, not
a flake.

Why a verdict object, not a raw boolean
=======================================

A "did the replay match?" boolean is enough for a CI gate but too
sparse for a human operator chasing a regression. The verdict carries
the diff -- entries the recorded manifest had that the replay
dropped, and entries the replay produced that the recording lacked --
plus the recomputed redacted view so a reviewer can see exactly which
strings the new policy treats differently. The schema is frozen
Pydantic v2 so an integration that serialises the verdict (e.g. the
CI gate's JSON output) cannot mutate it after construction.

Tenant scoping
==============

The audit row is loaded through the active
:class:`~sqlalchemy.ext.asyncio.AsyncSession` filtered on
``(id, tenant_id)`` -- the same posture every other audit-query helper
takes. A cross-tenant id is structurally indistinguishable from "no
such id" and surfaces as :data:`PolicyReplayStatus.AUDIT_ROW_NOT_FOUND`,
so policy-replay cannot leak the existence of another tenant's audit
rows.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import AuditLog
from meho_backplane.redaction.engine import RedactionManifestEntry, redact
from meho_backplane.redaction.resolver import find_policy_by_id

__all__ = [
    "PolicyReplayResult",
    "PolicyReplayStatus",
    "replay_policy",
]


class PolicyReplayStatus(StrEnum):
    """Terminal outcome of one :func:`replay_policy` call.

    Distinguishes the three failure modes -- audit row absent,
    policy missing, replay diverged -- so a consumer (the CI gate
    on the parallel #1073 work; an operator surface; a unit test)
    can switch exhaustively on the verdict without reparsing the
    diff list.
    """

    #: The replay re-produced the recorded manifest verbatim. The
    #: policy stays deterministic against this audit row's
    #: ``raw_payload``; redaction has not regressed for this case.
    MATCH = "match"

    #: The replay produced a different manifest. Inspect
    #: :attr:`PolicyReplayResult.missing` and :attr:`PolicyReplayResult.extra`
    #: for the divergence; this is the policy-regression signal the
    #: C1-d round-trip CI gate (#1073) fires on.
    DIVERGED = "diverged"

    #: No audit row exists for the requested id within the caller's
    #: tenant. Distinct from ``POLICY_NOT_FOUND`` so an operator
    #: surface can render "you don't own that row" vs "your policy
    #: was retired" differently.
    AUDIT_ROW_NOT_FOUND = "audit_row_not_found"

    #: The audit row exists but carries no ``redaction_policy_id`` /
    #: ``raw_payload`` -- typically a pre-G11.4-T2 row (#1071) written
    #: before the redaction middleware shipped, or an error-path row
    #: where the handler raised before producing a response. Replay
    #: is structurally impossible; this signals "not applicable"
    #: rather than masquerading as a successful match.
    REPLAY_NOT_APPLICABLE = "replay_not_applicable"

    #: The audit row records a ``redaction_policy_id`` no longer
    #: resolvable via :func:`~meho_backplane.redaction.resolver.find_policy_by_id`
    #: -- the YAML file has been retired or the row was written by a
    #: different deployment. The replay cannot run; this is distinct
    #: from a divergence so the operator knows the diagnosis is
    #: "policy was removed", not "policy regressed".
    POLICY_NOT_FOUND = "policy_not_found"


class PolicyReplayResult(BaseModel):
    """Verdict of one :func:`replay_policy` invocation.

    Frozen so a verdict handed to a caller (the CI gate, a CLI verb,
    a test assertion) cannot mutate after construction. The schema is
    deliberately shallow -- two manifest-entry lists plus the
    recomputed redacted view -- so a serialiser (CI's JSON output) can
    render it without recursing into engine internals.

    :attr:`missing` are entries the recorded manifest had that the
    replay dropped (a rule no longer fires); :attr:`extra` are entries
    the replay produced that the recording lacked (a new rule fires
    where the original did not). The lists carry the full
    :class:`~meho_backplane.redaction.engine.RedactionManifestEntry`
    so a reviewer reading the diff sees the rule, the pattern, the
    path, and the reason -- not just "something changed".

    :attr:`replayed_redacted` is the recomputed
    :func:`~meho_backplane.redaction.engine.redact` output (same
    JSON-shaped containers the engine always returns). When
    :attr:`status` is :data:`PolicyReplayStatus.MATCH` this is
    structurally identical to the redacted view the agent originally
    saw (modulo the audit row's storage shape); for
    :data:`PolicyReplayStatus.DIVERGED` it is the *new* redacted view
    the current policy would produce, which is the load-bearing
    artefact for diagnosing what changed in operator-visible strings.

    :attr:`policy_id` is the resolved policy id the replay ran against
    -- mirrored onto the verdict so a CI gate output can correlate
    diff entries to the policy diff. ``None`` for the
    :data:`PolicyReplayStatus.AUDIT_ROW_NOT_FOUND` /
    :data:`PolicyReplayStatus.REPLAY_NOT_APPLICABLE` paths where the
    policy was never identified.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    status: PolicyReplayStatus
    audit_id: uuid.UUID
    policy_id: str | None = None
    missing: tuple[RedactionManifestEntry, ...] = Field(default_factory=tuple)
    extra: tuple[RedactionManifestEntry, ...] = Field(default_factory=tuple)
    replayed_redacted: object | None = None


async def replay_policy(
    audit_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> PolicyReplayResult:
    """Re-run the recorded redaction policy against the row's raw payload.

    The policy-replay sense the G11.4-T5 #1074 acceptance criteria
    require: load the audit row, look its policy up by id, run
    :func:`~meho_backplane.redaction.engine.redact` against the
    recorded ``raw_payload``, and compare the new manifest to the
    stored one. An empty diff (:attr:`PolicyReplayResult.missing` and
    :attr:`PolicyReplayResult.extra` both empty) means the redactor
    stays deterministic for this row; a non-empty diff is the policy-
    regression signal.

    Tenant-scoping is enforced on the row load: the function filters
    on ``(id, tenant_id)`` so a cross-tenant id resolves the same as
    an unknown id (:data:`PolicyReplayStatus.AUDIT_ROW_NOT_FOUND`).
    The tenant comes from the validated JWT at the boundary
    (REST / MCP / CLI), never from caller-supplied filter input.

    Args:
        audit_id: The :class:`~meho_backplane.db.models.AuditLog` row
            to replay.
        tenant_id: The mandatory tenant boundary -- sourced from the
            validated JWT, never from caller input.
        session: An open :class:`AsyncSession`. The function only
            reads, never commits.

    Returns:
        A :class:`PolicyReplayResult`. See :class:`PolicyReplayStatus`
        for the terminal outcomes.

    The function never raises for a missing row, missing policy, or
    non-replayable row -- those map to explicit verdict statuses.
    Engine-level exceptions (an unknown pattern name a Pydantic
    schema validator should have caught) propagate verbatim; they
    indicate a corrupted policy registration that the caller's
    framework should surface, not a replay verdict.
    """
    row = await session.scalar(
        select(AuditLog).where(
            AuditLog.id == audit_id,
            AuditLog.tenant_id == tenant_id,
        ),
    )
    if row is None:
        return PolicyReplayResult(
            status=PolicyReplayStatus.AUDIT_ROW_NOT_FOUND,
            audit_id=audit_id,
        )

    policy_id = _extract_policy_id(row.payload)
    if policy_id is None or row.raw_payload is None:
        # Pre-G11.4-T2 (#1071) rows have no captured raw payload; an
        # error-path row (handler raised before producing a response)
        # also has none. Either way, there is nothing to replay.
        return PolicyReplayResult(
            status=PolicyReplayStatus.REPLAY_NOT_APPLICABLE,
            audit_id=audit_id,
            policy_id=policy_id,
        )

    policy = find_policy_by_id(policy_id)
    if policy is None:
        return PolicyReplayResult(
            status=PolicyReplayStatus.POLICY_NOT_FOUND,
            audit_id=audit_id,
            policy_id=policy_id,
        )

    # Re-run the engine with the same labels the middleware passed at
    # write time, so any scope-filtered rules behave identically. The
    # ``connector_id`` / ``op`` labels live on the audit row's
    # payload; the ``tenant`` label comes off the row itself (string-
    # coerced to match the resolver's
    # ``Annotated[str | None]`` field shape, the same coercion the
    # dispatcher's ``_apply_redaction_middleware`` applies on the hot
    # path).
    payload = row.payload if isinstance(row.payload, dict) else {}
    connector_id = _maybe_str(payload.get("connector_impl_id"))
    op_id = _maybe_str(payload.get("op_id"))
    tenant_label = str(row.tenant_id) if row.tenant_id is not None else None
    result = redact(
        row.raw_payload,
        policy,
        connector_id=connector_id,
        tenant=tenant_label,
        op=op_id,
    )

    recorded_manifest = _coerce_recorded_manifest(row.redaction_manifest)
    replayed_manifest = result.manifest
    missing, extra = _diff_manifests(recorded_manifest, replayed_manifest)
    status = PolicyReplayStatus.MATCH if not missing and not extra else PolicyReplayStatus.DIVERGED
    return PolicyReplayResult(
        status=status,
        audit_id=audit_id,
        policy_id=policy_id,
        missing=missing,
        extra=extra,
        replayed_redacted=result.redacted,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_policy_id(payload: object) -> str | None:
    """Pull ``redaction_policy_id`` out of an ``audit_log.payload`` dict.

    Defensive against non-dict payloads (the column is JSON-shaped but
    a legacy migration could in principle store something else) and
    non-string values. Returns ``None`` on either, which the caller
    maps to :data:`PolicyReplayStatus.REPLAY_NOT_APPLICABLE`.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("redaction_policy_id")
    return value if isinstance(value, str) and value else None


def _maybe_str(value: object) -> str | None:
    """Coerce *value* to a non-empty str, else ``None``.

    The redaction engine's scope labels are
    ``Annotated[str | None]``; an integer / boolean from the audit
    payload would crash the engine's predicate. Defensive coercion
    keeps the policy-replay sense robust against payload-shape drift.
    """
    return value if isinstance(value, str) and value else None


def _coerce_recorded_manifest(
    stored: object,
) -> tuple[RedactionManifestEntry, ...]:
    """Re-hydrate the audit row's manifest JSON into engine entries.

    The middleware writes the manifest as a list of dicts (via
    :func:`~meho_backplane.redaction.middleware.manifest_to_audit_payload`)
    so the audit-write encoder accepts it without per-row Pydantic
    round-tripping. Replay needs the engine's
    :class:`~meho_backplane.redaction.engine.RedactionManifestEntry`
    Pydantic shape so manifest-to-manifest comparison is structural,
    not string-based.

    A ``None`` / non-list stored value (a pre-G11.4-T2 row, or a
    storage-shape regression) yields an empty tuple -- the diff path
    will then surface every replayed entry as "extra" rather than
    silently swallowing the divergence.
    """
    if not isinstance(stored, list):
        return ()
    entries: list[RedactionManifestEntry] = []
    for raw in stored:
        if not isinstance(raw, dict):
            continue
        entries.append(RedactionManifestEntry.model_validate(raw))
    return tuple(entries)


def _entry_key(entry: RedactionManifestEntry) -> tuple[Any, ...]:
    """Stable hashable key for set-difference over manifest entries.

    Two manifest entries are *the same* when every load-bearing field
    -- the rule, the pattern that fired, the action, the path, the
    count, the span, and the reason -- match. ``span`` is a tuple of
    two ints; ``count`` is an int; everything else is a string, so
    the resulting key hashes cleanly under :class:`set`. The order
    inside this key is documentation, not load-bearing -- the diff
    only checks equality.
    """
    return (
        entry.rule,
        entry.pattern,
        entry.action,
        entry.path,
        entry.count,
        entry.span,
        entry.reason,
    )


def _diff_manifests(
    recorded: tuple[RedactionManifestEntry, ...],
    replayed: tuple[RedactionManifestEntry, ...],
) -> tuple[
    tuple[RedactionManifestEntry, ...],
    tuple[RedactionManifestEntry, ...],
]:
    """Return ``(missing, extra)`` -- the symmetric manifest difference.

    *missing* are entries present in *recorded* but absent from
    *replayed* -- a rule that fired at write time no longer does. The
    operator-visible regression mode this catches is "we used to
    redact this; we now leak it".

    *extra* are entries present in *replayed* but absent from
    *recorded* -- a rule fires now that did not at write time. This
    catches the dual regression: "we now redact something we used to
    let through", which is rarely a security regression but is
    usually a behaviour change worth flagging in CI.

    Ordering inside each tuple matches the engine's traversal order
    (a manifest is iterated front-to-back); the duplication discipline
    here keeps the function pure and deterministic.
    """
    recorded_keys = {_entry_key(entry) for entry in recorded}
    replayed_keys = {_entry_key(entry) for entry in replayed}
    missing = tuple(entry for entry in recorded if _entry_key(entry) not in replayed_keys)
    extra = tuple(entry for entry in replayed if _entry_key(entry) not in recorded_keys)
    return missing, extra
