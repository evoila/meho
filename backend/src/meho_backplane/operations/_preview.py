# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op ``proposed_effect`` builder hook for the approval-park path.

G11.7 follow-up (#1437). When the policy gate routes a dispatch to
``needs-approval`` (:func:`~meho_backplane.operations.dispatcher.dispatch`
Step 4 → :func:`_handle_needs_approval`), most ops have nothing to show
the reviewer beyond the call's identity, so the durable
:class:`~meho_backplane.db.models.ApprovalRequest` row stores the
identifier-only default ``{op_id, connector_id, target_id}``.

A small set of ops *can* compute a side-effect-free **preview** of what
the approved call would do -- notably ``k8s.apply``'s server-side-apply
dry-run (``dry_run="server"`` → the API's ``?dryRun=All``), which returns
the would-be object without persisting anything. This module is the
general, opt-in seam that lets such an op populate
``ApprovalRequest.proposed_effect`` at queue time, so the human reviewer
reads the diff in the approval queue rather than only in the
post-approval op result.

Design contract
---------------

* **Opt-in bespoke builder, generic echo default.** A builder is
  registered against an ``op_id`` to compute a richer preview (a
  computed dry-run, before/after specs, …). An op *without* a registered
  builder no longer falls straight through to the identifier-only
  default: it gets a **generic params-echo default** (#1856) that echoes
  the requested params -- redaction-safe -- so every approval-gated op
  has param-level legibility for free. The identifier-only default is
  reached only when the op is credential-class (suppressed), its params
  are empty, or the params dict normalises to nothing to show.
* **Fail-soft, never silent.** A builder that raises (a dry-run that
  hits the API server and errors, a transient connector fault) must
  never block the park: :func:`build_proposed_effect` swallows the
  exception and logs it. Parking an approval is the durable,
  safety-relevant action; a missing preview is a
  degraded-but-acceptable outcome, an exception is not. But the
  degradation is **visible** (#1628): the hook returns an explicit
  ``{op_class, preview_unavailable: True, preview_error}`` marker
  rather than ``None``, so the reviewer can tell "blast-radius
  unknown" from a genuinely small action. Only a *declined* preview
  (builder returns ``None``, credential-class suppression, or an empty
  params dict with no builder) yields ``None`` → the identifier-only
  default. The ``preview_error`` message is redacted with the packaged
  default policy before it is truncated, because a builder fault can
  echo an upstream error body (#3876).
* **Redaction-safe.** The preview lands in a durable row surfaced over
  REST / MCP / CLI, so it must not carry secret material. The hook
  reuses the single-sourced sensitivity classification from
  :func:`~meho_backplane.broadcast.events.classify_op` (shipped by
  G11.7-T1 #1401): an op that classifies as a credential class
  (``credential_read`` / ``credential_mint`` / ``credential_write`` /
  ``credential_use``) gets
  **no generic params-echo default** -- the generic echo can only do
  key-name / value-shape redaction and is not trusted to scrub a
  connector-specific secret shape, so a credential-class op with no
  bespoke builder collapses to the identifier-only default the same way
  the broadcast layer collapses such ops. A *bespoke* builder is the
  deliberate exception (#1857): it is trusted to own its field discipline
  and runs even for a credential-class op (the keycloak user-create
  preview scrubs the inline password via the connector's own
  ``redact_secret_fields`` before returning), mirroring the
  permission-preflight hook which likewise runs for credential-class ops.
  Builders are themselves expected to return identity-only / scrubbed
  summaries (``k8s.apply``'s dry-run echoes resource identity +
  ``resourceVersion`` + ``uid``, never Secret ``data``), so the
  classify-op gate is defence-in-depth for the generic path, not the
  only guard.

The k8s.apply builder is wired here (the only op in scope per #1437);
additional ops register their own builders as the need arises (argocd
writes are the separate follow-up #1452).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from meho_backplane.broadcast.events import (
    has_snmp_word,
    is_secret_name_value_pair,
    is_secret_param_name,
    is_snmp_community_key,
    resolve_op_class,
)
from meho_backplane.op_classes import CREDENTIAL_OP_CLASSES
from meho_backplane.redaction import apply_connector_boundary_redaction, get_default_policy, redact
from meho_backplane.settings import parse_bool_env

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.base import Connector
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "BLAST_RADIUS_KEY",
    "INTERNAL_EFFECT_KEYS",
    "PERSON_ONLY_EFFECT_KEYS",
    "PREVIEW_REASON_CREDENTIAL_REDACTED",
    "PREVIEW_REASON_NOT_POPULATED",
    "SWITCHED_EFFECT_KEYS",
    "PreviewContext",
    "approved_preview",
    "approved_preview_var",
    "blast_radius_missing_reason",
    "build_card_warnings",
    "build_permission_preflight",
    "build_proposed_effect",
    "describe_preview_provenance",
    "grant_preview",
    "grant_preview_var",
    "has_permission_preflight",
    "has_preview_builder",
    "preview_failure_reason",
    "proposed_effect_for_agent",
    "register_agent_preview_filter",
    "register_card_warnings",
    "register_permission_preflight",
    "register_person_only_preview_keys",
    "register_preview_builder",
    "register_preview_builder_switch",
    "register_preview_failure_keys",
    "register_preview_last_step",
    "without_internal_effect_keys",
    "without_person_only_effect_keys",
    "without_switched_object_field",
]

_log = structlog.get_logger(__name__)

#: Sensitivity classes whose preview must never be stored verbatim.
#: Mirrors the aggregate-only set in
#: :func:`~meho_backplane.broadcast.events.redact_payload`.
_SENSITIVE_CLASSES: frozenset[str] = CREDENTIAL_OP_CLASSES

#: Reviewer-facing reason code (#2332) for a ``proposed_effect`` that
#: collapsed to the op-identity-only default *because the op is a
#: credential class with no bespoke builder* — the value redaction is
#: intentional, not a missing preview. Distinguishes "asked to approve a
#: deliberately-redacted credential write" from "the connector never
#: populated a preview" so the approval surface can style it as
#: elevated-risk rather than merely incomplete.
PREVIEW_REASON_CREDENTIAL_REDACTED = "credential_write_redacted"

#: Reviewer-facing reason code (#2332) for a ``proposed_effect`` that
#: collapsed to the op-identity-only default for a *non*-credential op —
#: no builder registered and nothing to echo (empty params), or a builder
#: that declined. The preview is absent, not deliberately redacted.
PREVIEW_REASON_NOT_POPULATED = "connector_did_not_populate"

#: Sentinel substituted for a scrubbed secret-bearing param value. A
#: non-empty marker (rather than dropping the key) keeps the param shape
#: legible -- the reviewer sees that a ``password`` field *existed* on the
#: parked call without learning its value. Mirrors the keycloak read-op
#: scrub sentinel (:data:`~meho_backplane.connectors.keycloak.redaction.REDACTED`).
_PARAM_REDACTED = "***REDACTED***"

#: Truncation bound for the reviewer-facing ``preview_error`` reason. A
#: builder fault is normally a one-line message; the cap keeps a
#: pathological exception repr (an HTTP error echoing a response body)
#: from ballooning the durable approval row.
_PREVIEW_ERROR_MAX_LEN: int = 500

#: Fail-closed placeholder for the ``preview_error`` message when the
#: redactor itself fails. The marker still says the preview is
#: unavailable; only the message is withheld. Returning the raw text
#: instead would be the leak this redaction exists to close.
_PREVIEW_ERROR_WITHHELD: str = "<message withheld: redaction unavailable>"

#: The park-time preview of the request that is being re-run after its
#: approval, as ``(op_id, preview)``: *op_id* is the parked op and *preview*
#: the dict its builder returned (``{}`` when the park stored no computed
#: preview). Bound by
#: :func:`~meho_backplane.operations.approval_queue.composite_resume_binding`
#: on both resume paths for the whole re-run, so a handler that resolves a
#: name at park time can act on the object the approver saw instead of
#: looking the name up again (#5202). ``None`` for every ordinary dispatch.
approved_preview_var: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar(
    "approved_preview",
    default=None,
)


#: The fresh preview MEHO built for a ``destructive`` call that a delete grant
#: clears, as ``(op_id, preview)``. *preview* holds the ``blast_radius`` block
#: of that preview, the shape of an approval card. Set by
#: :func:`~meho_backplane.operations.dispatcher.destructive_binding_evidence`
#: when its checks pass; each dispatch binds ``None`` first and puts the
#: caller's value back when it ends. ``None`` for every other dispatch.
grant_preview_var: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar(
    "grant_preview",
    default=None,
)


def grant_preview(op_id: str) -> dict[str, Any] | None:
    """The fresh preview of *op_id* that cleared its delete grant, or ``None``."""
    bound = grant_preview_var.get()
    if bound is None or bound[0] != op_id:
        return None
    return bound[1]


def approved_preview(op_id: str) -> dict[str, Any] | None:
    """The preview the approver saw for *op_id*, during the approved re-run of that op.

    ``None`` on an ordinary dispatch and when the re-run is for a different
    op (a sub-op park re-runs its parent composite; the parent must not read
    the sub-op's preview as its own). An empty dict means the call was
    approved but its park stored no computed preview.
    """
    bound = approved_preview_var.get()
    if bound is None or bound[0] != op_id:
        return None
    return bound[1]


def preview_failure_reason(exc: Exception) -> str:
    """Render a builder fault as the short reviewer-facing reason string.

    Public so a builder that keeps a failed read inside its preview (the VM
    relocate copy-size preview, #4749) renders the reason the same way.

    Keeps the exception *message* front and centre (the type name alone
    is opaque -- the lesson of the ``connector_error`` flattening,
    sibling #1627) while staying robust for message-less exceptions.

    The message is redacted **before** it is truncated, with the same
    packaged default policy the ``connector_error`` envelope uses for
    ``extras.upstream_message``. A builder fault can echo an upstream
    body: a Kubernetes admission webhook that denies a dry-run may put a
    password or token into its ``Status`` message (#3876), and the
    marker is stored in the durable approval row. Redaction runs first
    so the cap cannot cut a secret into a fragment the patterns no
    longer match. If the redactor fails, the message is withheld.
    """
    message = str(exc)
    if message:
        message = _redact_preview_message(message)
    reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(reason) > _PREVIEW_ERROR_MAX_LEN:
        reason = reason[:_PREVIEW_ERROR_MAX_LEN] + " [truncated]"
    return reason


def _redact_preview_message(message: str) -> str:
    """Tier-1-redact one builder fault message; withhold it on any failure."""
    try:
        redacted = redact(message, get_default_policy()).redacted
    except Exception:  # fail-closed: this path must never leak and never raise
        return _PREVIEW_ERROR_WITHHELD
    if not isinstance(redacted, str):  # pragma: no cover -- str in, str out
        return _PREVIEW_ERROR_WITHHELD
    return redacted


def _scrub_secret_param_keys(value: Any, *, in_snmp: bool = False) -> Any:
    """Replace secret-by-name param values with :data:`_PARAM_REDACTED`.

    Walks any mapping and any sequence (other than strings and bytes)
    recursively, the same container types the broadcast params scrub
    walks, so a tuple of dicts hides its secrets the same way a list
    does. For every mapping, a key that
    :func:`~meho_backplane.broadcast.events.is_secret_param_name` marks as
    secret has its whole value replaced when that value is a string, a
    mapping or a sequence -- so a nested ``{"credentials": [...]}`` shows
    no element. The ``value`` of a name/value pair whose ``name`` is secret
    (:func:`~meho_backplane.broadcast.events.is_secret_name_value_pair`)
    is replaced the same way. This is the same name check and the same value rule the
    broadcast params scrub uses, so a param the broadcast path flags as
    secret is always masked here too (``password``, ``admin_password``,
    ``rootPassword``, ``client_secret``, ``api_key``, ...). Bare scalars
    (bool / int / float / ``None``) under a secret-shaped name pass
    through, like on the broadcast path: secret material is a string or a
    blob, never ``bind_secret_id=true`` or ``secret_id_ttl=600``, and the
    approver needs to see those settings. Every other value is walked so
    a secret keyed deep inside the params tree is still caught. The input
    is never mutated; a new structure is returned.

    A bare ``community`` is hidden in an SNMP place: next to an SNMP field,
    or under a key with an SNMP word (*in_snmp*,
    :func:`~meho_backplane.broadcast.events.is_snmp_community_key`).

    This is the key-name half of the generic echo's redaction; the
    value-shape half is :func:`apply_connector_boundary_redaction`.
    """
    if isinstance(value, Mapping):
        hide_pair_value = is_secret_name_value_pair(value)
        return {
            key: (
                _PARAM_REDACTED
                if isinstance(key, str)
                and (
                    is_secret_param_name(key)
                    or is_snmp_community_key(value, key, in_snmp=in_snmp)
                    or (hide_pair_value and key == "value")
                )
                and isinstance(item, (str, Mapping, Sequence))
                else _scrub_secret_param_keys(item, in_snmp=in_snmp or has_snmp_word(key))
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_scrub_secret_param_keys(item, in_snmp=in_snmp) for item in value]
    return value


def _redact_echoed_params(
    params: dict[str, Any],
    *,
    connector_id: str | None,
    tenant: str | None,
    op_id: str,
) -> dict[str, Any]:
    """Scrub a params dict for the generic params-echo default.

    Two passes, both reusing the existing redaction discipline:

    1. **Key-name scrub** (:func:`_scrub_secret_param_keys`) -- masks
       values keyed by a secret-shaped name, using the same name check as
       the broadcast params scrub. The connector-boundary
       engine walks Mappings key-by-key but matches secret *value* shapes
       only, so a structured ``{"password": "hunter2"}`` would otherwise
       echo verbatim.
    2. **Value-shape scrub** (:func:`apply_connector_boundary_redaction`)
       -- the same connector-boundary pipeline the response path and the
       dispatch-request preview (:mod:`._request_preview`) run, catching
       JWTs / bearer tokens / kubeconfig blobs / labelled ``key=value``
       secrets embedded in a string leaf. Run *after* the key-name pass so
       a masked sentinel is what the engine sees, never the raw secret.

    Returns a JSON-shaped dict (the engine normalises models / tuples).
    """
    key_scrubbed = _scrub_secret_param_keys(params)
    redaction = apply_connector_boundary_redaction(
        key_scrubbed,
        connector_id=connector_id,
        tenant=tenant,
        op=op_id,
    )
    # The engine normalises a dict input to a dict (``.redacted`` is typed
    # ``Any``); narrow it so the echo envelope value stays well-typed.
    redacted = redaction.redacted
    return redacted if isinstance(redacted, dict) else {}


def _generic_params_echo(ctx: PreviewContext, *, op_class: str) -> dict[str, Any] | None:
    """Build the generic params-echo default for an op with no builder (#1856).

    Echoes the requested params under a ``params_echo`` envelope key
    (distinct from a computed ``preview``) after the two-pass redaction in
    :func:`_redact_echoed_params`, so the approval surface can tell a
    generic echo from a bespoke preview. Empty params carry no more
    legibility than the identifier-only default and collapse to ``None``
    (the caller's fallback). Credential-class suppression is handled by the
    caller *before* this runs, so a secret-class op never reaches here.
    """
    if not ctx.params:
        return None
    tenant = str(ctx.operator.tenant_id) if ctx.operator.tenant_id is not None else None
    return {
        "op_class": op_class,
        "params_echo": _redact_echoed_params(
            ctx.params,
            connector_id=ctx.connector_id,
            tenant=tenant,
            op_id=ctx.descriptor.op_id,
        ),
    }


@dataclass(frozen=True)
class PreviewContext:
    """Everything a preview builder needs to compute a dry-run.

    The dispatcher assembles this at the approval-park point and hands it
    to the registered builder. The builder owns the connector call (e.g.
    re-invoking the op's handler with a dry-run flag forced on); the
    dispatcher only resolves the connector instance + descriptor.

    Attributes:
        descriptor: The looked-up :class:`EndpointDescriptor` for the op.
        connector_instance: The resolved connector singleton, or ``None``
            for module-level handlers that bind no connector.
        operator: The authenticated operator whose dispatch parked.
        target: The dispatch target (or ``None`` for tenant-wide ops).
        params: The original dispatch params.
        connector_id: The call's natural-key connector id (e.g.
            ``"vault-1.x"``), used to resolve the per-connector
            :class:`~meho_backplane.redaction.policy.RedactionPolicy` for
            the generic params-echo default. ``None`` (the default) falls
            back to the conservative connector-boundary default policy --
            the same behaviour as an unmatched override.
        agent_preview: ``True`` when the agent's own ``preview_operation``
            asks, ``False`` (the default) for the approval request. A
            builder may show the approver more than the agent; what the
            agent gets beyond that follows ``PREVIEW_TYPED_WRITES``. A
            builder switch (:func:`register_preview_builder_switch`)
            applies only to the agent's side.
    """

    descriptor: EndpointDescriptor
    connector_instance: Connector | None
    operator: Operator
    target: Any
    params: dict[str, Any]
    connector_id: str | None = None
    agent_preview: bool = False


class PreviewBuilder(Protocol):
    """An op's opt-in preview computation.

    Returns the preview dict to store in
    :attr:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`, or
    ``None`` to decline (caller falls back to the identifier-only
    default). May raise -- :func:`build_proposed_effect` treats a raise
    as "preview unavailable" (an explicit marker on the parked row,
    #1628) rather than failing the park.
    """

    async def __call__(self, ctx: PreviewContext) -> dict[str, Any] | None: ...


class CardWarnings(Protocol):
    """Gives the warning lines for the approval card of one op.

    The lines are plain text for the approver. They name the target and the
    setting, and hold no secret value. Return ``[]`` when there is none.
    """

    async def __call__(self, ctx: PreviewContext) -> list[str]: ...


class PreviewLastStep(Protocol):
    """A step that runs on the preview of an op after its builder.

    It may change the preview in place. A step that raises makes the
    preview unavailable, the same as a builder that raises.
    """

    async def __call__(self, ctx: PreviewContext, preview: dict[str, Any]) -> None: ...


class PermissionPreflight(Protocol):
    """An op's opt-in park-time permission check.

    Distinct from :class:`PreviewBuilder`: a preview computes *what the
    write would do* (request/response shape) and is therefore suppressed
    for credential-class ops; a permission preflight checks only *whether
    the dispatching identity is authorized to perform the write* and
    carries **no secret material** (e.g. Vault's ``sys/capabilities-self``
    returns capability names, never secret values). It is therefore run
    for every op with a registered preflight regardless of sensitivity
    class -- the credential-class suppression that gates previews does
    **not** apply here.

    Returns a redaction-safe summary dict to merge into
    :attr:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`, or
    ``None`` to decline (op isn't covered, or the probe failed soft). May
    raise -- :func:`build_permission_preflight` treats a raise as "no
    preflight" rather than failing the park.
    """

    async def __call__(self, ctx: PreviewContext) -> dict[str, Any] | None: ...


#: ``op_id`` -> builder of the warning lines on the approval card.
_CARD_WARNINGS: dict[str, CardWarnings] = {}

#: ``op_id`` -> registered preview builder. Opt-in: absence ⇒ no preview.
_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {}

#: ``op_id`` -> the step that runs on the builder's preview (:func:`register_preview_last_step`).
_PREVIEW_LAST_STEPS: dict[str, PreviewLastStep] = {}

#: ``op_id`` -> the switch of its builder for the caller's side. Off, the
#: preview the caller gets back is the one of an op with no builder; the
#: approval request a person reads always uses the builder.
_PREVIEW_BUILDER_SWITCHES: dict[str, Callable[[], bool]] = {}

#: ``op_id`` -> the switch, and the keys its builder adds to the preview for
#: the person only. Off, a caller reads the stored preview without them.
_PERSON_ONLY_PREVIEW_KEYS: dict[str, tuple[Callable[[], bool], frozenset[str]]] = {}

#: ``op_id`` -> a function that takes the stored preview and gives back the
#: preview a caller reads (:func:`register_agent_preview_filter`).
_AGENT_PREVIEW_FILTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

#: ``op_id`` -> a function that gives the keys a failed preview of the op
#: still shows a person (:func:`register_preview_failure_keys`).
_PREVIEW_FAILURE_KEYS: dict[str, Callable[[PreviewContext], dict[str, Any]]] = {}

#: The keys a builder's result puts on a stored ``proposed_effect``.
_BUILDER_RESULT_KEYS: Final[tuple[str, ...]] = ("preview", "preview_unavailable", "preview_error")

#: ``op_id`` -> registered permission preflight. Opt-in: absence ⇒ no
#: preflight (and no entry in the approval row's ``permission_preflight``).
_PERMISSION_PREFLIGHTS: dict[str, PermissionPreflight] = {}


def register_preview_builder(op_id: str, builder: PreviewBuilder) -> None:
    """Register *builder* as the preview computation for *op_id*.

    Idempotent re-registration overwrites -- registration happens at
    import time so a re-import (test reload) is a no-op-equivalent.
    """
    _PREVIEW_BUILDERS[op_id] = builder


def register_preview_last_step(op_id: str, step: PreviewLastStep) -> None:
    """Run *step* on every preview the builder of *op_id* returns.

    The step runs after the builder, whatever module registered the
    builder and when. It does not run when the builder returns ``None``,
    or when no builder runs (for example for the caller while the
    builder's switch is off).
    """
    _PREVIEW_LAST_STEPS[op_id] = step


def register_preview_builder_switch(op_id: str, enabled: Callable[[], bool]) -> None:
    """Use the builder of *op_id* for the caller's preview only while ``enabled()`` is true.

    Off, the preview the caller gets back (``preview_operation``) is the one
    of an op with no builder (the generic params echo). The approval request
    a person reads always uses the builder. A new card for an op the agent
    already sees goes behind a switch this way.
    """
    _PREVIEW_BUILDER_SWITCHES[op_id] = enabled


def register_person_only_preview_keys(
    op_id: str, enabled: Callable[[], bool], keys: frozenset[str]
) -> None:
    """Show the preview *keys* of *op_id* to a caller only while ``enabled()`` is true.

    The builder adds them to the approval request a person reads. Off, a
    caller that reads the stored request (:func:`proposed_effect_for_agent`)
    gets the preview without them.
    """
    _PERSON_ONLY_PREVIEW_KEYS[op_id] = (enabled, keys)


def register_preview_failure_keys(
    op_id: str, build: Callable[[PreviewContext], dict[str, Any]]
) -> None:
    """Add the keys *build* gives to the failed preview of *op_id*, for a person only.

    When the builder of *op_id* raises, the stored request holds the
    ``preview_unavailable`` marker. *build* adds what the card can still show
    from the params alone. MEHO keeps only the keys of
    :data:`PERSON_ONLY_EFFECT_KEYS`, so a caller that is not a person never
    reads them. The agent's own preview gets none, and a *build* that raises
    adds nothing.
    """
    _PREVIEW_FAILURE_KEYS[op_id] = build


def register_agent_preview_filter(
    op_id: str, preview_filter: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    """Give a caller the stored preview of *op_id* as *preview_filter* returns it.

    For person-only fields deeper in the preview than its top-level keys
    (:func:`register_person_only_preview_keys`). The filter reads its own
    switch and gives back a new dict; it never changes the stored one.
    """
    _AGENT_PREVIEW_FILTERS[op_id] = preview_filter


#: Top-level keys of a stored ``proposed_effect`` that only a person reads:
#: the line of the approval card that says why a credential call waits, and
#: what each container of a ``k8s.apply`` runs when its dry run failed
#: (:func:`register_preview_failure_keys`).
PERSON_ONLY_EFFECT_KEYS: Final[frozenset[str]] = frozenset({"credential_wait", "run_commands"})

#: Top-level keys of a stored ``proposed_effect`` that a caller reads only
#: while a switch is on, each with its env name. A person always reads them:
#: the realm a Keycloak target works on.
SWITCHED_EFFECT_KEYS: Final[dict[str, str]] = {"target_realm": "KEYCLOAK_ANSWER_REALM"}

#: ``op_id`` -> (a field of the card's ``blast_radius.object`` that a caller
#: reads only while a switch is on, the env name of the switch). MEHO compares
#: the stored field before an approved run, and a person always reads it.
SWITCHED_OBJECT_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "keycloak.realm.delete": ("id", "KEYCLOAK_ANSWER_REALM")
}

#: Top-level keys of a stored ``proposed_effect`` that MEHO keeps for its own
#: checks and never returns to a caller: where a change of rights was asked
#: (:data:`~meho_backplane.operations.grant_approval.ASKED_IN_WEB_CONSOLE`).
INTERNAL_EFFECT_KEYS: Final[frozenset[str]] = frozenset({"asked_in_web_console"})


def without_internal_effect_keys(effect: dict[str, Any]) -> dict[str, Any]:
    """*effect* without :data:`INTERNAL_EFFECT_KEYS`; *effect* itself when it has none."""
    if INTERNAL_EFFECT_KEYS.isdisjoint(effect):
        return effect
    return {key: value for key, value in effect.items() if key not in INTERNAL_EFFECT_KEYS}


def _switch_on(env: str) -> bool:
    return parse_bool_env(os.environ.get(env))


def without_switched_object_field(op_id: Any, block: Any) -> Any:
    """The ``blast_radius`` *block* of *op_id* as a caller reads it.

    Without the field of :data:`SWITCHED_OBJECT_FIELDS` while its switch is
    off. *block* itself when there is nothing to hide.
    """
    field, env = SWITCHED_OBJECT_FIELDS.get(op_id, ("", ""))
    obj = block.get("object") if isinstance(block, dict) else None
    if not isinstance(obj, dict) or field not in obj or _switch_on(env):
        return block
    return {**block, "object": {key: value for key, value in obj.items() if key != field}}


def _hide_switched_object_field(effect: dict[str, Any]) -> dict[str, Any]:
    """*effect* with both copies of its ``blast_radius`` as a caller reads them."""
    op_id = effect.get("op_id")
    if op_id not in SWITCHED_OBJECT_FIELDS:
        return effect

    def _shown(holder: dict[str, Any]) -> dict[str, Any]:
        if BLAST_RADIUS_KEY not in holder:
            return holder
        block = without_switched_object_field(op_id, holder[BLAST_RADIUS_KEY])
        return {**holder, BLAST_RADIUS_KEY: block}

    preview = effect.get("preview")
    if isinstance(preview, dict):
        effect = {**effect, "preview": _shown(preview)}
    return _shown(effect)


def without_person_only_effect_keys(effect: dict[str, Any]) -> dict[str, Any]:
    """*effect* without :data:`PERSON_ONLY_EFFECT_KEYS` and :data:`INTERNAL_EFFECT_KEYS`.

    Also without each key of :data:`SWITCHED_EFFECT_KEYS` and each field of
    :data:`SWITCHED_OBJECT_FIELDS` whose switch is off. *effect* itself when
    it has none of them.
    """
    effect = _hide_switched_object_field(effect)
    off = {key for key, env in SWITCHED_EFFECT_KEYS.items() if not _switch_on(env)}
    hidden = PERSON_ONLY_EFFECT_KEYS | INTERNAL_EFFECT_KEYS | off
    if hidden.isdisjoint(effect):
        return effect
    return {key: value for key, value in effect.items() if key not in hidden}


def proposed_effect_for_agent(
    op_id: str,
    effect: dict[str, Any] | None,
    *,
    params: Mapping[str, Any] | None,
    connector_id: str | None,
    tenant: str | None,
) -> dict[str, Any] | None:
    """The stored ``proposed_effect`` of an approval request, as a caller reads it.

    The request a person reads always holds the builder's card. A caller
    (``meho_approvals_get`` / ``meho_approvals_list``) gets what
    ``preview_operation`` gives while a card's switch is off:

    * the generic params echo of the stored params, for an op whose builder
      switch is off (:func:`register_preview_builder_switch`);
    * the preview without the person-only keys
      (:func:`register_person_only_preview_keys`);
    * the preview as the op's filter gives it back
      (:func:`register_agent_preview_filter`), after the person-only keys
      are gone.

    The top-level person-only keys (:data:`PERSON_ONLY_EFFECT_KEYS`) and
    the internal keys (:data:`INTERNAL_EFFECT_KEYS`) are never in it. A key
    of :data:`SWITCHED_EFFECT_KEYS` or a field of :data:`SWITCHED_OBJECT_FIELDS`
    is in it only while its switch is on. Every other ``proposed_effect`` comes
    back unchanged.
    """
    if not isinstance(effect, dict):
        return effect
    effect = without_person_only_effect_keys(effect)
    switch = _PREVIEW_BUILDER_SWITCHES.get(op_id)
    if switch is not None and not switch() and any(key in effect for key in _BUILDER_RESULT_KEYS):
        shown = {key: value for key, value in effect.items() if key not in _BUILDER_RESULT_KEYS}
        shown.pop("preview_reason", None)
        if params:
            shown["params_echo"] = _redact_echoed_params(
                dict(params), connector_id=connector_id, tenant=tenant, op_id=op_id
            )
        populated, reason = describe_preview_provenance(
            shown if params else None, op_id=op_id, stored_op_class=shown.get("op_class")
        )
        shown["preview_populated"] = populated
        if reason is not None:
            shown["preview_reason"] = reason
        return shown
    preview = effect.get("preview")
    if not isinstance(preview, dict):
        return effect
    person_only = _PERSON_ONLY_PREVIEW_KEYS.get(op_id)
    preview_filter = _AGENT_PREVIEW_FILTERS.get(op_id)
    if person_only is None and preview_filter is None:
        return effect
    if person_only is not None and not person_only[0]():
        preview = {key: value for key, value in preview.items() if key not in person_only[1]}
    if preview_filter is not None:
        preview = preview_filter(preview)
    return {**effect, "preview": preview}


def _preview_builder(op_id: str, *, for_agent: bool) -> PreviewBuilder | None:
    """The builder of *op_id*; ``None`` when it has none, or for the caller when it is off."""
    switch = _PREVIEW_BUILDER_SWITCHES.get(op_id)
    if for_agent and switch is not None and not switch():
        return None
    return _PREVIEW_BUILDERS.get(op_id)


def has_preview_builder(op_id: str) -> bool:
    """True when *op_id* has a bespoke preview builder for the caller's preview."""
    return _preview_builder(op_id, for_agent=True) is not None


def register_permission_preflight(op_id: str, preflight: PermissionPreflight) -> None:
    """Register *preflight* as the park-time permission check for *op_id*.

    Idempotent re-registration overwrites -- registration happens at
    import time so a re-import (test reload) is a no-op-equivalent.
    """
    _PERMISSION_PREFLIGHTS[op_id] = preflight


def register_card_warnings(op_id: str, builder: CardWarnings) -> None:
    """Register *builder*: the warning lines for the approval card of *op_id*.

    Used by a write that changes a setting which is allowed but risky, so the
    approver sees the warning before approving. Re-registration overwrites.
    """
    _CARD_WARNINGS[op_id] = builder


async def build_card_warnings(ctx: PreviewContext) -> list[str]:
    """The warning lines a registered builder gives for this call; ``[]`` for none.

    A warning is a help for the approver. A builder that fails never blocks
    the park: the failure is logged and the card has no such line.
    """
    builder = _CARD_WARNINGS.get(ctx.descriptor.op_id)
    if builder is None:
        return []
    try:
        return list(await builder(ctx))
    except Exception:
        _log.warning("card_warnings_failed", op_id=ctx.descriptor.op_id, exc_info=True)
        return []


def has_permission_preflight(op_id: str) -> bool:
    """True when *op_id* has a park-time permission check registered."""
    return op_id in _PERMISSION_PREFLIGHTS


async def build_proposed_effect(ctx: PreviewContext) -> dict[str, Any] | None:
    """Compute the ``proposed_effect`` preview for a parking dispatch.

    Resolution order:

    1. **Bespoke builder, if registered** for ``ctx.descriptor.op_id``.
       Its result is wrapped ``{op_class, preview}``; ``None`` from the
       builder declines to the identifier-only default. Richer than the
       generic echo (a computed dry-run, before/after specs, …). A bespoke
       builder runs **even for a credential-class op** (#1857): like the
       permission-preflight hook, it is trusted to own its own field
       discipline (e.g. the keycloak user-create preview scrubs the inline
       password before returning), so the credential-class suppression in
       step 3 does not apply to it.
    2. **Generic params-echo default** when no builder is registered
       (#1856) *and* the op is not credential-class. The requested params
       are echoed under a ``params_echo`` envelope key (distinct from a
       computed ``preview``) after a two-pass redaction -- key-name scrub +
       connector-boundary value-shape scrub (:func:`_redact_echoed_params`)
       -- so every approval-gated op gets param-level legibility for free
       without leaking secret material. Empty params carry no more
       legibility than the identifier-only default and collapse to ``None``.
    3. **Credential-class suppression** for a credential-class op
       (:data:`_SENSITIVE_CLASSES`) with **no** bespoke builder: the
       generic echo can only do generic redaction and cannot be trusted
       to scrub a connector-specific secret shape, so it is refused and
       the caller falls back to the identifier-only default. Returns
       ``None``.

    A builder that **raises** is still fail-soft -- the park proceeds
    regardless -- but no longer silent (#1628): the hook returns an
    explicit ``{"op_class", "preview_unavailable": True,
    "preview_error"}`` marker so the reviewer-facing row can say
    "blast-radius unknown" instead of degrading to a bare identifier
    default indistinguishable from a small action. The dispatcher
    merges the marker onto the identifier fields before parking.

    On builder success the returned dict is wrapped with a ``"preview"``
    envelope key + the op's sensitivity ``"op_class"`` so the approval
    surface can label it as a computed preview rather than the bare
    identifier default; the generic default uses ``"params_echo"`` in
    place of ``"preview"``.
    """
    op_id = ctx.descriptor.op_id
    op_class = resolve_op_class(op_id, getattr(ctx.descriptor, "op_class", None))
    builder = _preview_builder(op_id, for_agent=ctx.agent_preview)
    if op_class in _SENSITIVE_CLASSES and builder is None:
        # A credential-class op with NO bespoke builder must not surface
        # request/response detail in a durable row -- the *generic*
        # params-echo default below is suppressed because it can only do
        # generic key-name / value-shape redaction and cannot be trusted
        # to scrub a connector-specific secret shape. Refuse outright; the
        # reviewer still sees the identifier-only default via the caller's
        # fallback. A *bespoke* builder (below) is the deliberate
        # exception: like the permission-preflight hook
        # (:func:`build_permission_preflight`), it is trusted to own its
        # own field discipline and may run for a credential-class op (e.g.
        # the keycloak user-create preview, #1857, scrubs the inline
        # password via the connector's own ``redact_secret_fields``).
        _log.info(
            "proposed_effect_preview_suppressed",
            op_id=op_id,
            op_class=op_class,
            reason="sensitive_op_class_no_builder",
        )
        return None

    if builder is None:
        # No bespoke builder, not credential-class: fall back to the generic
        # params-echo default so every approval-gated op gets param-level
        # legibility for free.
        return _generic_params_echo(ctx, op_class=op_class)

    try:
        preview = await builder(ctx)
        last_step = _PREVIEW_LAST_STEPS.get(op_id)
        if preview is not None and last_step is not None:
            await last_step(ctx, preview)
    except Exception as exc:
        # Fail-soft: a preview is a convenience, the park is the
        # safety-relevant action. Never let a dry-run fault block it.
        marker = _preview_unavailable_marker(ctx, exc, op_id=op_id, op_class=op_class)
        if not ctx.agent_preview:
            marker.update(_preview_failure_keys(ctx, op_id=op_id, marker=marker))
        return marker

    if preview is None:
        return None

    return {
        "op_class": op_class,
        "preview": preview,
    }


def _preview_unavailable_marker(
    ctx: PreviewContext, exc: Exception, *, op_id: str, op_class: str
) -> dict[str, Any]:
    """Log a builder fault and return the ``preview_unavailable`` marker.

    Never silent (#1628): a registered builder that raised means the
    blast radius is UNKNOWN, and the reviewer must be able to
    distinguish that from a genuinely small action -- so the row carries
    an explicit marker + reason instead of the bare identifier-only
    default a server-side log line can't substitute for.

    The log line carries the same redacted reason as the row, not the
    raw exception: a builder fault can echo an upstream body (#3876).
    """
    reason = preview_failure_reason(exc)
    _log.warning(
        "proposed_effect_preview_failed",
        op_id=op_id,
        operator_sub=getattr(ctx.operator, "sub", None),
        preview_error=reason,
    )
    return {
        "op_class": op_class,
        "preview_unavailable": True,
        "preview_error": reason,
    }


def _preview_failure_keys(
    ctx: PreviewContext, *, op_id: str, marker: dict[str, Any]
) -> dict[str, Any]:
    """The person-only keys a failed preview of *op_id* still shows.

    Only keys of :data:`PERSON_ONLY_EFFECT_KEYS` that the marker does not
    hold yet. Empty when *op_id* registered none or its function raised.
    """
    build = _PREVIEW_FAILURE_KEYS.get(op_id)
    if build is None:
        return {}
    try:
        keys = build(ctx)
    except Exception as exc:
        _log.warning(
            "proposed_effect_failure_keys_failed",
            op_id=op_id,
            error=preview_failure_reason(exc),
        )
        return {}
    return {
        key: value
        for key, value in keys.items()
        if key in PERSON_ONLY_EFFECT_KEYS and key not in marker
    }


def describe_preview_provenance(
    preview: dict[str, Any] | None, *, op_id: str, stored_op_class: str | None = None
) -> tuple[bool, str | None]:
    """Classify a built ``proposed_effect`` envelope for the reviewer surface (#2332).

    Returns ``(preview_populated, preview_reason)`` from the value
    :func:`build_proposed_effect` produced (before the identifier-only
    default is merged in), so the dispatcher can stamp both onto every
    parked-request envelope:

    * ``preview_populated`` — ``True`` when a real bespoke ``preview`` or
      generic ``params_echo`` landed; ``False`` when the envelope
      collapsed to the op-identity-only default (a caller can then refuse
      to auto-approve the blind case). A builder that *raised* (the
      ``preview_unavailable`` marker, #1628) counts as **not** populated —
      the blast radius is unknown, not shown.
    * ``preview_reason`` — set only when ``preview_populated`` is ``False``
      and the op is not the fault case:

      - :data:`PREVIEW_REASON_CREDENTIAL_REDACTED` when the op is a
        credential class (its preview is intentionally suppressed absent a
        bespoke builder).
      - :data:`PREVIEW_REASON_NOT_POPULATED` otherwise (no builder /
        nothing to echo).

      ``None`` when the preview is populated, or when the builder faulted
      (that state carries its own ``preview_unavailable`` / ``preview_error``
      marker and is not a "reason" for an intentionally-sparse preview).
    """
    if preview is not None and preview.get("preview_unavailable") is True:
        # The builder ran but faulted — a distinct signaled state that
        # already carries its own marker + reason; not "populated", and
        # not an intentionally-sparse-preview reason code.
        return False, None
    if preview is not None and ("preview" in preview or "params_echo" in preview):
        return True, None
    # Collapsed to the identifier-only default: name WHY so the approver
    # sees a deliberately-redacted credential write distinctly from a
    # connector that simply never populated a preview.
    if resolve_op_class(op_id, stored_op_class) in _SENSITIVE_CLASSES:
        return False, PREVIEW_REASON_CREDENTIAL_REDACTED
    return False, PREVIEW_REASON_NOT_POPULATED


#: Top-level ``proposed_effect`` key carrying the mandatory blast-radius
#: statement for the ``destructive`` tier (#3197, decision requirement 3).
#: A destructive op's preview builder returns it inside its preview dict;
#: :func:`~meho_backplane.operations.dispatcher._build_proposed_effect`
#: promotes it to the top level (alongside ``safety_level`` / ``op_class``)
#: so the reviewer surface and the mandatory-block gate read it without
#: reaching into ``preview``.
BLAST_RADIUS_KEY: Final[str] = "blast_radius"


def blast_radius_missing_reason(proposed_effect: Mapping[str, Any] | None) -> str | None:
    """Return ``None`` when a valid blast-radius block is present, else why not.

    The mandatory-block gate for the ``destructive`` tier (#3197, decision
    requirement 3). A destructive op cannot park with only the
    identifier-only default: its ``proposed_effect`` must carry a
    :data:`BLAST_RADIUS_KEY` block naming the three things the approver
    reads before deciding —

    * ``object`` -- the identity of what is destroyed (truthy);
    * ``children`` -- the enumerated child objects that go with it (a list;
      empty is valid — "no children", explicitly enumerated, is a
      statement, not an omission);
    * ``irreversibility`` -- the irreversibility class (a non-empty string).

    Returns a human-readable reason naming the first missing / malformed
    field so the dispatcher's refuse-to-park result is actionable; ``None``
    means the block is well-formed and the park may proceed.

    When the block is absent because the op's preview builder raised, the
    reason also carries the builder's error (the ``preview_error`` marker,
    #1628). A builder raises on purpose to refuse a delete before it waits
    for approval (for example a protected object, #3944), so the caller must
    be able to read why.
    """
    if not proposed_effect:
        return "proposed_effect is empty (no blast-radius statement)"
    block = proposed_effect.get(BLAST_RADIUS_KEY)
    if not isinstance(block, Mapping):
        preview_error = proposed_effect.get("preview_error")
        if isinstance(preview_error, str) and preview_error:
            return f"the preview builder failed: {preview_error}"
        return f"proposed_effect.{BLAST_RADIUS_KEY} block is absent"
    if not block.get("object"):
        return f"{BLAST_RADIUS_KEY}.object (object identity) is missing"
    if not isinstance(block.get("children"), list):
        return f"{BLAST_RADIUS_KEY}.children (enumerated child objects) must be a list"
    irreversibility = block.get("irreversibility")
    if not isinstance(irreversibility, str) or not irreversibility:
        return f"{BLAST_RADIUS_KEY}.irreversibility (irreversibility class) is missing"
    return None


async def build_permission_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Run the op's park-time permission check, if one is registered.

    G0.20-T4 (#1504). Looks up a preflight for ``ctx.descriptor.op_id``;
    returns ``None`` (no entry added to the approval row) when no
    preflight is registered, when the preflight declines, or when it
    raises (fail-soft -- the park proceeds regardless, mirroring
    :func:`build_proposed_effect`).

    Unlike :func:`build_proposed_effect`, this hook does **not** consult
    the credential-class suppression set: a permission preflight returns
    only authorization metadata (capability names, never a secret value),
    so suppressing it for credential-class ops would defeat the whole
    point -- a Vault ``vault.kv.put`` (which *is* ``credential_write``)
    is exactly the op whose write Vault may deny post-approval. The
    preflight is the redaction-safe way to surface that at park time.

    On success the returned dict is stored under the ``"permission_preflight"``
    key of the approval row's ``proposed_effect`` envelope so the
    approval surface can render a "this write will be denied" banner.
    """
    op_id = ctx.descriptor.op_id
    preflight = _PERMISSION_PREFLIGHTS.get(op_id)
    if preflight is None:
        return None

    try:
        result = await preflight(ctx)
    except Exception:
        # Fail-soft: a preflight is an early-warning convenience; the park
        # is the safety-relevant action. Never let a probe fault block it.
        _log.warning(
            "permission_preflight_failed",
            op_id=op_id,
            operator_sub=getattr(ctx.operator, "sub", None),
            exc_info=True,
        )
        return None

    return result


async def _k8s_apply_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``k8s.apply`` -- the server-side-apply dry-run.

    Re-invokes the ``k8s_apply`` handler with ``dry_run="server"`` forced
    on (the API's ``?dryRun=All``) so nothing is persisted; the returned
    per-document summary (resource identity + ``resourceVersion`` +
    ``uid``) is the diff-preview the reviewer reads. The handler echoes
    no Secret ``data`` -- it operates on the manifest's GVK + metadata --
    so the summary is identity-only and redaction-safe. On top of that
    summary the builder adds the redacted manifest and a ``subject`` line
    (:func:`_k8s_apply_manifest_echo`, #3877), so the reviewer sees what
    the apply sets, not only which objects it touches.

    Returns ``None`` when no connector instance resolved (a k8s.apply
    without a target can't dry-run) so the caller falls back to the
    identifier-only default.

    When the API server rejects the dry-run (an admission webhook denial,
    an invalid object, a conflict), the handler raises
    :class:`~meho_backplane.connectors.kubernetes.ops_write_dangerous.ApplyRejectedError`
    and this builder lets it propagate. :func:`build_proposed_effect` then
    stores the ``preview_unavailable`` marker with the server's message in
    ``preview_error``, so the reviewer sees that the apply will be
    rejected instead of a clean-looking preview with no
    ``resourceVersion`` and no ``uid`` (#3876). For a person, that marker
    still names what each container runs (:func:`_k8s_apply_failure_keys`).
    """
    if ctx.connector_instance is None or ctx.target is None:
        return None

    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.ops_write_dangerous import k8s_apply

    if not isinstance(ctx.connector_instance, KubernetesConnector):
        return None

    # Force the dry-run flag on regardless of what the caller passed:
    # the preview must never persist, even if the parked call itself was
    # a real apply (dry_run="none").
    preview_params = {**ctx.params, "dry_run": "server"}
    result = await k8s_apply(
        ctx.connector_instance,
        ctx.target,
        ctx.operator,
        preview_params,
    )
    return {**result, **_k8s_apply_manifest_echo(ctx, result)}


def _k8s_apply_manifest_echo(ctx: PreviewContext, result: dict[str, Any]) -> dict[str, Any]:
    """The ``subject`` and ``manifest`` keys the k8s.apply preview adds (#3877).

    The dry-run summary names each object but not what the apply sets on
    it. ``manifest`` is the applied manifest, one dict per document, with
    Secret values and ``env`` values hidden by the connector, then run
    through the same two-pass redaction as the generic params echo. The
    connector applies its size limit after that redaction, so the stored
    echo is what is measured. ``subject`` names the objects for the
    reviewer context. ``run_commands`` names what each container runs, with
    secret values hidden (``ops_write_preview.apply_run_commands``). Only a
    person reads it: an agent's own preview has no such key, and the
    stored request an agent reads leaves it out.
    """
    from meho_backplane.connectors.kubernetes.ops_write_preview import (
        RUN_COMMANDS_KEY,
        apply_manifest_echo,
        apply_run_commands,
        apply_subject,
    )

    _redact = _k8s_apply_redactor(ctx)
    echo: dict[str, Any] = {"manifest": apply_manifest_echo(ctx.params["manifest"], redact=_redact)}
    if not ctx.agent_preview:
        run_commands = apply_run_commands(ctx.params["manifest"], redact=_redact)
        if run_commands:
            echo[RUN_COMMANDS_KEY] = run_commands
    subject = apply_subject(result.get("applied"))
    if subject is not None:
        echo["subject"] = subject
    return echo


def _k8s_apply_redactor(
    ctx: PreviewContext,
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """The shared params redaction, run over a list of manifest dicts of *ctx*."""
    tenant = str(ctx.operator.tenant_id) if ctx.operator.tenant_id is not None else None

    def _redact(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        redacted = _redact_echoed_params(
            {"manifest": docs},
            connector_id=ctx.connector_id,
            tenant=tenant,
            op_id=ctx.descriptor.op_id,
        )
        return [doc for doc in redacted.get("manifest", []) if isinstance(doc, dict)]

    return _redact


def _k8s_apply_failure_keys(ctx: PreviewContext) -> dict[str, Any]:
    """``run_commands`` for the card of a ``k8s.apply`` whose dry run failed.

    MEHO builds it from the manifest text, the same as on a preview that
    worked (``ops_write_preview.apply_run_commands``). The card still says
    that the preview is not available.
    """
    from meho_backplane.connectors.kubernetes.ops_write_preview import (
        RUN_COMMANDS_KEY,
        apply_run_commands,
    )

    manifest = ctx.params.get("manifest")
    if not isinstance(manifest, str):
        return {}
    run_commands = apply_run_commands(manifest, redact=_k8s_apply_redactor(ctx))
    return {RUN_COMMANDS_KEY: run_commands} if run_commands else {}


async def _vault_kv_write_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Permission preflight for the KV-v2 write ops (G0.20-T4 #1504).

    Delegates to
    :func:`~meho_backplane.connectors.vault.ops.vault_kv_write_capability_preflight`,
    which logs in under the **resolved per-target role** the write will
    use (#3274 — the dispatch ``target``'s ``extras["vault_role"]``, or
    the settings-global ``meho-mcp`` role when the target names none) and
    issues ``POST sys/capabilities-self`` on the op's authorization path
    to learn whether that token holds the capabilities the write needs
    (``create`` / ``update`` for ``put``; ``read`` / ``update`` for
    ``patch``, which reads before it writes). Threading ``ctx.target`` is what makes the
    banner reflect the role that will actually execute. The response
    carries only capability names -- no secret value -- so it is
    redaction-safe and runs even though ``vault.kv.put`` /
    ``vault.kv.patch`` classify as ``credential_write``.
    """
    from meho_backplane.connectors.vault.ops import vault_kv_write_capability_preflight

    return await vault_kv_write_capability_preflight(
        ctx.operator,
        ctx.descriptor.op_id,
        ctx.params,
        target=ctx.target,
    )


async def _k8s_secret_read_to_ref_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Permission preflight for ``k8s.secret.read_to_ref`` (#3844).

    The op reads a Kubernetes Secret and writes the value to Vault through
    the ``vault.kv.put`` handler. Its preview is suppressed (it classifies
    ``credential_read``), so without this check the approver could not see
    that Vault will refuse the write. This computes the same destination
    the handler writes to -- ``tenant_secret_ref(operator.tenant_id,
    register_as)`` on the default KV mount -- and runs the ``vault.kv.put``
    capability probe there. It passes ``target=None`` on purpose: the
    handler writes with ``target=None`` (the global Vault role), and
    ``ctx.target`` is the Kubernetes cluster, not a Vault target.
    """
    register_as = ctx.params.get("register_as")
    if not isinstance(register_as, str):
        return None

    from meho_backplane.connectors._shared.vault_creds import DEFAULT_KV_MOUNT
    from meho_backplane.connectors.vault.ops import vault_kv_write_capability_preflight
    from meho_backplane.connectors.vault.tenant_paths import tenant_secret_ref

    dest_ref = tenant_secret_ref(ctx.operator.tenant_id, register_as)
    return await vault_kv_write_capability_preflight(
        ctx.operator,
        "vault.kv.put",
        {"mount": DEFAULT_KV_MOUNT, "path": dest_ref},
        target=None,
    )


def _register_builtin_builders() -> None:
    """Wire the in-tree preview builders + permission preflights.

    Called at import time. ``k8s.apply`` registers a side-effect-free
    dry-run preview (#1437); the KV-v2 write ops register a
    capability-only permission preflight (#1504) -- a different hook
    because a credential-class op's preview is suppressed but its
    permission check (capability names, no secret) is not.
    ``k8s.secret.read_to_ref`` registers the same probe on the Vault path
    it stages to (#3844).
    """
    register_preview_builder("k8s.apply", _k8s_apply_preview)
    register_preview_failure_keys("k8s.apply", _k8s_apply_failure_keys)
    for _write_op in (
        "vault.kv.put",
        "vault.kv.patch",
        "vault.kv.generate",
        "vault.kv.delete",
        "vault.kv.metadata.delete",
    ):
        register_permission_preflight(_write_op, _vault_kv_write_preflight)
    register_permission_preflight("k8s.secret.read_to_ref", _k8s_secret_read_to_ref_preflight)


_register_builtin_builders()
