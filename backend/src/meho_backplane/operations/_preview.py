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
  (``credential_read`` / ``credential_mint`` / ``credential_write``) gets
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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from meho_backplane.broadcast.events import classify_op, is_secret_param_name
from meho_backplane.redaction import apply_connector_boundary_redaction, get_default_policy, redact

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.base import Connector
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "BLAST_RADIUS_KEY",
    "PREVIEW_REASON_CREDENTIAL_REDACTED",
    "PREVIEW_REASON_NOT_POPULATED",
    "PreviewContext",
    "blast_radius_missing_reason",
    "build_permission_preflight",
    "build_proposed_effect",
    "describe_preview_provenance",
    "preview_failure_reason",
    "register_permission_preflight",
    "register_preview_builder",
]

_log = structlog.get_logger(__name__)

#: Sensitivity classes whose preview must never be stored verbatim.
#: Mirrors the aggregate-only set in
#: :func:`~meho_backplane.broadcast.events.redact_payload`.
_SENSITIVE_CLASSES: frozenset[str] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)

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


def _scrub_secret_param_keys(value: Any) -> Any:
    """Replace secret-by-name param values with :data:`_PARAM_REDACTED`.

    Walks any mapping and any sequence (other than strings and bytes)
    recursively, the same container types the broadcast params scrub
    walks, so a tuple of dicts hides its secrets the same way a list
    does. For every mapping, a key that
    :func:`~meho_backplane.broadcast.events.is_secret_param_name` marks as
    secret has its whole value replaced when that value is a string, a
    mapping or a sequence -- so a nested ``{"credentials": [...]}`` leaks
    no element. This is the same name check and the same value rule the
    broadcast params scrub uses, so a param the broadcast path flags as
    secret is always masked here too (``password``, ``admin_password``,
    ``rootPassword``, ``client_secret``, ``api_key``, ...). Bare scalars
    (bool / int / float / ``None``) under a secret-shaped name pass
    through, like on the broadcast path: secret material is a string or a
    blob, never ``bind_secret_id=true`` or ``secret_id_ttl=600``, and the
    approver needs to see those settings. Every other value is walked so
    a secret keyed deep inside the params tree is still caught. The input
    is never mutated; a new structure is returned.

    This is the key-name half of the generic echo's redaction; the
    value-shape half is :func:`apply_connector_boundary_redaction`.
    """
    if isinstance(value, Mapping):
        return {
            key: (
                _PARAM_REDACTED
                if isinstance(key, str)
                and is_secret_param_name(key)
                and isinstance(item, (str, Mapping, Sequence))
                else _scrub_secret_param_keys(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_scrub_secret_param_keys(item) for item in value]
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
    """

    descriptor: EndpointDescriptor
    connector_instance: Connector | None
    operator: Operator
    target: Any
    params: dict[str, Any]
    connector_id: str | None = None


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


#: ``op_id`` -> registered preview builder. Opt-in: absence ⇒ no preview.
_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {}

#: ``op_id`` -> registered permission preflight. Opt-in: absence ⇒ no
#: preflight (and no entry in the approval row's ``permission_preflight``).
_PERMISSION_PREFLIGHTS: dict[str, PermissionPreflight] = {}


def register_preview_builder(op_id: str, builder: PreviewBuilder) -> None:
    """Register *builder* as the preview computation for *op_id*.

    Idempotent re-registration overwrites -- registration happens at
    import time so a re-import (test reload) is a no-op-equivalent.
    """
    _PREVIEW_BUILDERS[op_id] = builder


def register_permission_preflight(op_id: str, preflight: PermissionPreflight) -> None:
    """Register *preflight* as the park-time permission check for *op_id*.

    Idempotent re-registration overwrites -- registration happens at
    import time so a re-import (test reload) is a no-op-equivalent.
    """
    _PERMISSION_PREFLIGHTS[op_id] = preflight


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
    op_class = classify_op(op_id)
    builder = _PREVIEW_BUILDERS.get(op_id)
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
    except Exception as exc:
        # Fail-soft: a preview is a convenience, the park is the
        # safety-relevant action. Never let a dry-run fault block it.
        return _preview_unavailable_marker(ctx, exc, op_id=op_id, op_class=op_class)

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


def describe_preview_provenance(
    preview: dict[str, Any] | None, *, op_id: str
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
    if classify_op(op_id) in _SENSITIVE_CLASSES:
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
    so the summary is identity-only and redaction-safe.

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
    ``resourceVersion`` and no ``uid`` (#3876).
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
    return await k8s_apply(
        ctx.connector_instance,
        ctx.target,
        ctx.operator,
        preview_params,
    )


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
    for _write_op in (
        "vault.kv.put",
        "vault.kv.patch",
        "vault.kv.delete",
        "vault.kv.metadata.delete",
    ):
        register_permission_preflight(_write_op, _vault_kv_write_preflight)
    register_permission_preflight("k8s.secret.read_to_ref", _k8s_secret_read_to_ref_preflight)


_register_builtin_builders()
