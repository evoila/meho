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
* A ``keycloak`` side (a user password sink or a client secret source)
  names the Keycloak target it points to now (``target_id``,
  ``target_name``), how MEHO connects to it (``host``, ``port``, ``scheme``
  and the TLS check ``tls``) and the reference of its admin login
  (``secret_ref``). The approved run compares the id, these connection
  fields and the secret reference with the target it finds, before any
  Keycloak login, and refuses another one
  (:class:`~meho_backplane.connectors.keycloak.secret_move_target.KeycloakTargetChangedError`).
  ``preview_operation``, ``meho_approvals_get`` and ``meho_approvals_list``
  leave these fields out; only the card a person reads shows them.
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
and, for a ``vault-target`` side, the target name as typed. It makes no
target lookup and no Vault login.

A move with a ``keycloak`` side is a change of rights
=====================================================

Such a move sets a Keycloak user's password or reads a client secret, so
it counts as a change of rights, like ``keycloak.user.reset_password``
(:func:`~meho_backplane.operations.grant_approval.register_rights_change_check`
with :func:`has_keycloak_side`). The requester never approves it, and
another admin (a tenant admin or a platform admin) must. A move between
two Vault places is not a change of rights.

A move whose source or sink is in MEHO's own login realm needs a platform
admin as approver, and so does a move whose realm MEHO cannot read. The
card says it in its ``grant`` block
(:func:`~meho_backplane.connectors.keycloak.ops_write_platform_reach.secret_move_grant`,
registered with
:func:`~meho_backplane.operations.grant_approval.register_grant_op`). The
check runs again right before the approved move runs. A move approved
with a card made before this rule does not run
(:func:`~meho_backplane.operations.grant_approval.register_harmless_keys_rule`).

What a caller reads about a ``linux`` side is behind the switch
``SECRET_MOVE_LINUX_TARGET_CARD`` (``Settings.secret_move_linux_target_card``,
off by default). On, ``preview_operation`` adds the target name as typed,
and ``meho_approvals_get`` / ``meho_approvals_list`` show the five fields
of the card. Off, they leave them out
(:func:`~meho_backplane.operations._preview.proposed_effect_for_agent`).
The approval request a person reads and the check of the approved run do
not depend on the switch.

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

from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    agent_cards_on,
    secret_move_grant,
)
from meho_backplane.connectors.keycloak.secret_move_target import keycloak_side_card
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
    register_agent_preview_filter,
    register_permission_preflight,
    register_preview_builder,
)
from meho_backplane.operations._validate import params_problem, register_params_check
from meho_backplane.operations.grant_approval import (
    register_grant_op,
    register_harmless_keys_rule,
    register_rights_change_check,
)
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import AmbiguousTargetError, TargetNotFoundError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.secret.endpoints import SecretRef

__all__ = ["build_secret_move_preview", "check_secret_move_sink", "has_keycloak_side"]

_LINUX_KIND = "linux"
_KEYCLOAK_KIND = "keycloak"

#: The fields the approval request adds to a ``linux:`` side. A caller reads
#: them only while ``SECRET_MOVE_LINUX_TARGET_CARD`` is on.
_LINUX_CARD_KEYS: frozenset[str] = frozenset({"target", "target_id", "target_name", "host", "port"})

#: The fields the approval request adds to a ``keycloak:`` side. Only the card
#: a person reads shows them.
_KEYCLOAK_CARD_KEYS: frozenset[str] = frozenset(
    {"target_id", "target_name", "host", "port", "scheme", "tls", "secret_ref"}
)


def _ref_only(spec: SecretRef, *, linux_target: bool) -> dict[str, str]:
    """Render a parsed ``<kind>:<ref>`` as a ref-only summary dict.

    Names the store-selecting ``kind`` and the store-specific ``ref``
    separately so a reviewer reads the move's endpoints structurally. The
    ``ref`` is an address (a Vault KV path + ``#<field>`` fragment), never
    a value. No I/O.
    """
    summary = {"kind": spec.kind, "ref": spec.ref}
    if spec.kind == VAULT_TARGET_KIND or (spec.kind == _LINUX_KIND and linux_target):
        # The target this side names, on its own so a reviewer does not
        # have to cut it out of the ref.
        summary["target"] = spec.ref.partition("/")[0]
    return summary


def _linux_card_on() -> bool:
    """Whether a caller reads the ``linux:`` target fields (``SECRET_MOVE_LINUX_TARGET_CARD``)."""
    return get_settings().secret_move_linux_target_card


def _hide_target_cards(preview: dict[str, Any]) -> dict[str, Any]:
    """The stored preview as a caller reads it.

    No ``keycloak:`` target fields, no ``linux:`` target fields with
    ``SECRET_MOVE_LINUX_TARGET_CARD`` off, and no ``grant`` block with
    ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` off.
    """
    hidden = {_KEYCLOAK_KIND: _KEYCLOAK_CARD_KEYS}
    if not _linux_card_on():
        hidden[_LINUX_KIND] = _LINUX_CARD_KEYS
    shown = dict(preview)
    if not agent_cards_on():
        shown.pop("grant", None)
    for side in ("source", "sink"):
        summary = shown.get(side)
        if not isinstance(summary, dict):
            continue
        keys = hidden.get(str(summary.get("kind")))
        if keys is not None:
            shown[side] = {key: value for key, value in summary.items() if key not in keys}
    return shown


def has_keycloak_side(params: dict[str, Any]) -> bool:
    """Whether the source or the sink of a ``secret.move`` is a ``keycloak:`` ref. No I/O."""
    for key in ("from", "to"):
        raw = params.get(key)
        if not isinstance(raw, str):
            continue
        try:
            if parse_secret_ref(raw).kind == _KEYCLOAK_KIND:
                return True
        except ValueError:
            continue
    return False


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

    A ``keycloak:`` side also names the Keycloak target it points to now
    (``target_id``, ``target_name``), its ``host``, ``port``, ``scheme``,
    TLS check (``tls``) and ``secret_ref``. The approved run compares them
    with the target it finds.
    """
    summary = _ref_only(spec, linux_target=True)
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
    elif spec.kind == _KEYCLOAK_KIND:
        target_name = spec.ref.partition("/")[0].strip()
        summary.update(await keycloak_side_card(target_name, operator))
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
        summary["source"] = _ref_only(source, linux_target=_linux_card_on())
        summary["sink"] = _ref_only(sink, linux_target=_linux_card_on())
    else:
        summary["source"] = await _ref_summary(source, ctx.operator)
        summary["sink"] = await _ref_summary(sink, ctx.operator)
        summary["sink"].update(await _sink_state(sink, ctx.operator, ctx.params, merge=merge))
        grant = await secret_move_grant(ctx)
        if grant is not None:
            summary["grant"] = grant
    summary["merge"] = merge
    reason = ctx.params.get("reason")
    if isinstance(reason, str) and reason:
        summary["reason"] = reason
    return summary


def _register_secret_move_preview_builder() -> None:
    """Wire the ``secret.move`` preview builder and its checks. Import-time."""
    register_preview_builder("secret.move", build_secret_move_preview)
    register_permission_preflight("secret.move", vault_target_move_preflight)
    register_params_check("secret.move", check_secret_move_sink)
    register_agent_preview_filter("secret.move", _hide_target_cards)
    register_rights_change_check("secret.move", has_keycloak_side)
    register_grant_op("secret.move", applies=has_keycloak_side)
    register_harmless_keys_rule("secret.move")


_register_secret_move_preview_builder()
