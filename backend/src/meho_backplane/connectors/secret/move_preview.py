# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builder for ``secret.move``.

G0.22-T3 (#1579). Wires the secret-broker ``secret.move`` op onto the
per-op preview hook shipped by #1437
(:mod:`meho_backplane.operations._preview`). When the policy gate parks
an unapproved move at ``awaiting_approval``, this builder populates the
durable :class:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`
with a **ref-only** summary so the human reviewer sees *which* credential
is being moved *between which stores* — and nothing value-derived beyond
what the response already exposes.

Ref-only by construction
========================

The summary names only the parsed ``{kind, ref}`` of the move's
``--from`` / ``--to`` references (e.g. ``vault`` /
``secret/db/prod#password``). These come straight from the op params,
which carry references and never the value (#1577's core invariant:
``additionalProperties: false`` on the param schema keeps a value field
from being smuggled in, and the value is read server-side into a
redacting :class:`~.endpoints.SecretMaterial`). The builder never reads
the source. The optional operator ``reason`` is echoed (it is
operator-authored audit text the schema requires not to carry a value).

What the card adds to the refs
==============================

* A ``vault-target`` side names the target as typed (``target``), the
  target it points to now (``target_id``, ``target_name``), that target's
  Vault role (``vault_role``) and its Vault login mount (``vault_mount``).
  The approved run compares all three with the target it finds and
  refuses another target, role or mount
  (:class:`~meho_backplane.connectors.secret.vault_target_endpoint.VaultTargetChangedError`).
* A ``linux`` side names the target as typed (``target``), the Linux target
  it points to now (``target_id``, ``target_name``), its ``host`` and its
  SSH ``port``. The approved run compares the id, host and port with the
  target it finds and refuses another one
  (:class:`~meho_backplane.connectors.linux.secret_sink_target.LinuxTargetChangedError`).
* The sink says whether its secret exists (``secret_exists``) and, with
  ``merge``, whether the named field exists (``field_exists``): ``"yes"``,
  ``"no"`` or ``"not known"``. A Vault sink looks it up with the same
  login and checks as the write
  (:meth:`~meho_backplane.connectors.secret.vault_endpoint.VaultKvSecretEndpoint.sink_state`);
  it keeps only yes or no and drops the read data at once. Every other
  sink kind and every failed lookup say ``"not known"``. The approved run
  holds a Vault sink to a ``"no"``
  (:meth:`~meho_backplane.connectors.secret.vault_endpoint.VaultKvSecretEndpoint.write_secret`).

The agent's own ``preview_operation`` gets only the refs: ``{kind, ref}``
and, for a ``vault-target`` or ``linux`` side, the target name as typed. It
makes no target lookup and no Vault login.

Why ``secret.move`` reaches a builder at all
============================================

:func:`~meho_backplane.broadcast.events.classify_op` returns ``"other"``
for ``secret.move`` (``.move`` is in neither the read- nor the
write-suffix set, and ``secret.move`` is in no credential allowlist), so
the credential-class preview suppression in
:func:`~meho_backplane.operations._preview.build_proposed_effect` does
**not** fire — a registered builder runs and its ref-only dict lands on
the row. (For a credential-class op the preview is suppressed to ``None``;
``secret.move`` carries no value to suppress, so surfacing the refs is
both safe and the point.) ``build_proposed_effect`` wraps the returned
dict as ``{"op_class": "other", "preview": <this dict>}``.

A ``vault-target:`` side the caller may not use
==============================================

The op's permission preflight is
:func:`~meho_backplane.connectors.secret.vault_target_endpoint.vault_target_move_preflight`.
It checks the named Vault target (it must have its own role) and the
caller's policy on it. When it says no, the dispatcher refuses the call and
makes no approval card.

A ``linux:`` sink the path check refuses
========================================

The run would refuse such a sink, so the call never waits for approval:
:func:`check_secret_move_sink` is the op's params check
(:func:`~meho_backplane.operations._validate.register_params_check`). The
dispatcher and ``preview_operation`` refuse the call as ``invalid_params``
before the policy gate, so no approval card is made. The builder declines
for such a sink too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.linux.secret_endpoint import (
    LinuxSecretRefError,
    LinuxSecretSinkEndpoint,
    parse_linux_secret_ref,
)
from meho_backplane.connectors.linux.secret_sink_target import sink_target_card
from meho_backplane.connectors.secret.endpoints import parse_secret_ref, resolve_secret_endpoint
from meho_backplane.connectors.secret.vault_endpoint import NOT_KNOWN, VaultKvSecretEndpoint
from meho_backplane.connectors.secret.vault_target_endpoint import (
    VAULT_TARGET_KIND,
    VaultTargetKvSecretEndpoint,
    VaultTargetRefusedError,
    check_vault_target_refs,
    login_of,
    vault_target_move_preflight,
)
from meho_backplane.operations._preview import (
    PreviewContext,
    register_permission_preflight,
    register_preview_builder,
)
from meho_backplane.operations._validate import params_problem, register_params_check
from meho_backplane.targets.resolver import AmbiguousTargetError, TargetNotFoundError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.secret.endpoints import SecretRef

__all__ = ["build_secret_move_preview", "check_secret_move_sink"]

_LINUX_KIND = "linux"


def _ref_only(spec: SecretRef) -> dict[str, str]:
    """Render a parsed ``<kind>:<ref>`` as a ref-only summary dict.

    Names the store-selecting ``kind`` and the store-specific ``ref``
    separately so a reviewer reads the move's endpoints structurally. The
    ``ref`` is an address (a Vault KV path + ``#<field>`` fragment), never
    a value. No I/O.
    """
    summary = {"kind": spec.kind, "ref": spec.ref}
    if spec.kind in (VAULT_TARGET_KIND, _LINUX_KIND):
        # The target this side names, on its own so a reviewer does not
        # have to cut it out of the ref.
        summary["target"] = spec.ref.partition("/")[0]
    return summary


async def _linux_target_summary(spec: SecretRef, operator: Operator) -> dict[str, str]:
    """The Linux target a ``linux:`` side points to now: id, name, host and port.

    Empty when the name finds no Linux target (or more than one). The
    approved run then finds no target id on the card and refuses.
    """
    try:
        endpoint = LinuxSecretSinkEndpoint(spec.ref)
        row = await endpoint.check_target(operator)
    except (LinuxSecretRefError, TargetNotFoundError, AmbiguousTargetError):
        return {}
    return sink_target_card(row)


async def _ref_summary(spec: SecretRef, operator: Operator) -> dict[str, str]:
    """The approval card's summary of one side: :func:`_ref_only` and the target.

    A ``vault-target`` side also names the target it points to now
    (``target_id``, ``target_name``), that target's Vault role
    (``vault_role``) and its Vault login mount (``vault_mount``). The
    approved run compares them with the target it finds. A name that does
    not resolve to a usable Vault target gets no ``target_id``; the
    permission check then refuses the call.

    A ``linux:`` side also names the Linux target it points to now
    (``target_id``, ``target_name``), its ``host`` and its SSH ``port``.
    The approved run compares the id, host and port with the target it
    finds.
    """
    summary = _ref_only(spec)
    if spec.kind == VAULT_TARGET_KIND:
        endpoint = VaultTargetKvSecretEndpoint(spec.ref)
        try:
            row = await endpoint.resolve_vault_target(operator)
        except VaultTargetRefusedError:
            return summary
        summary["target_id"] = str(row.id)
        summary["target_name"] = row.name
        summary["vault_role"], summary["vault_mount"] = login_of(row)
    elif spec.kind == _LINUX_KIND:
        summary.update(await _linux_target_summary(spec, operator))
    return summary


async def _sink_state(
    spec: SecretRef,
    operator: Operator,
    params: dict[str, Any],
    *,
    merge: bool,
) -> dict[str, str]:
    """Whether the sink secret exists (and with *merge*, its field): yes, no or not known.

    Only a Vault sink can tell; every other sink kind says "not known". The
    lookup runs only when both ``vault-target`` sides pass their checks
    (:func:`check_vault_target_refs`), so a call MEHO refuses makes no
    Vault login.
    """
    sink: object | None
    try:
        sink = resolve_secret_endpoint(spec)
        await check_vault_target_refs(operator, params)
    except Exception:
        sink = None
    if isinstance(sink, VaultKvSecretEndpoint):
        return await sink.sink_state(operator, merge=merge)
    state = {"secret_exists": NOT_KNOWN}
    if merge:
        state["field_exists"] = NOT_KNOWN
    return state


def _linux_sink_refusal(raw_to: str) -> str | None:
    """The path check's reason when *raw_to* is a ``linux:`` place it refuses."""
    try:
        sink = parse_secret_ref(raw_to)
    except ValueError:
        return None  # the run reports a malformed ref itself
    if sink.kind != _LINUX_KIND:
        return None
    try:
        parse_linux_secret_ref(sink.ref)
    except ValueError as exc:
        return str(exc)
    return None


def check_secret_move_sink(params: dict[str, Any]) -> dict[str, Any] | None:
    """Params check of ``secret.move``: refuse a ``linux:`` sink the run would refuse.

    No I/O: the path check reads only the ref. The reason names the ref's
    parts (target, path, owner), never a value.
    """
    raw_to = params.get("to")
    if not isinstance(raw_to, str):
        return None
    reason = _linux_sink_refusal(raw_to)
    if reason is None:
        return None
    return params_problem(
        "to",
        "a 'linux:' place inside the home of an owner that is not root",
        reason,
    )


async def build_secret_move_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the ref-only ``proposed_effect`` preview for ``secret.move``.

    Names the move's source and sink as parsed ``{kind, ref}`` references
    plus the optional operator ``reason`` — all drawn from the params,
    which carry references only. The agent's own ``preview_operation``
    gets just that. For the approval request, a ``vault-target`` side also
    names the target it points to and that target's Vault role. The sink says
    whether its secret exists (``secret_exists``) and, with ``merge``,
    whether its field exists (``field_exists``): ``"yes"``, ``"no"`` or
    ``"not known"``. That lookup reads the sink secret's field names only;
    no value enters the summary. Declines (returns ``None`` → identifier-only default)
    when a ref is absent or malformed; the dispatcher has already
    schema-validated the params at this point, so that path is defensive.
    """
    raw_from = ctx.params.get("from")
    raw_to = ctx.params.get("to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        return None
    try:
        source = parse_secret_ref(raw_from)
        sink = parse_secret_ref(raw_to)
    except ValueError:
        return None
    if _linux_sink_refusal(raw_to) is not None:
        return None

    merge = ctx.params.get("merge") is True
    summary: dict[str, Any] = {"action": "secret.move"}
    if ctx.agent_preview:
        # The agent's own preview: the refs only, no lookup.
        summary["source"] = _ref_only(source)
        summary["sink"] = _ref_only(sink)
    else:
        summary["source"] = await _ref_summary(source, ctx.operator)
        summary["sink"] = await _ref_summary(sink, ctx.operator)
        summary["sink"].update(await _sink_state(sink, ctx.operator, ctx.params, merge=merge))
    summary["merge"] = merge
    reason = ctx.params.get("reason")
    if isinstance(reason, str) and reason:
        summary["reason"] = reason
    return summary


def _register_secret_move_preview_builder() -> None:
    """Wire the ``secret.move`` preview builder, permission check and params check. Import-time."""
    register_preview_builder("secret.move", build_secret_move_preview)
    register_permission_preflight("secret.move", vault_target_move_preflight)
    register_params_check("secret.move", check_secret_move_sink)


_register_secret_move_preview_builder()
