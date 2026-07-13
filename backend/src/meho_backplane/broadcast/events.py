# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Broadcast event schema + PII classifier (G6.1-T2).

The publish-on-write hook (T3, #309) builds one :class:`BroadcastEvent`
per audited operation and ``XADD``\\ s it to ``meho:feed:{tenant_id}``.
T4 (#310) reads back from the same stream and serves it via SSE; T6
(#312) wraps the stream in an MCP resource. This module ships the
wire-shape contract and the sensitivity classifier that every
downstream consumer relies on.

PII discipline lives in :func:`classify_op` + :func:`redact_payload`.
The classifier is policy-locked by decision #3 in
``docs/planning/v0.2-decisions.md`` — credential reads and audit-query
responses broadcast aggregate-only by default; everything else
broadcasts in full. Per-op opt-in to flip a sensitive class to full
detail is a G6.3 surface; T2 ships the conservative default.

Why a classifier rather than a per-op annotation:

1. Sensitivity is mostly op-class-shaped. ``vault.kv.read`` is no more
   sensitive than ``vault.kv.list``; ``vsphere.vm.list`` is no more
   sensitive than ``vsphere.host.list``. A per-op flag would multiply
   the contract surface for no policy gain.
2. The classifier is one auditable function. A reviewer can read it in
   one sitting and verify policy compliance; scattered annotations on
   every op would require a registry walk.

Why aggregate-only-by-default for the sensitive classes:

* ``credential_read`` — even logging that ``vault.kv.read`` returned OK
  reveals which secret an operator touched. Path strings frequently
  carry environment names, target hostnames, or service identifiers
  that no SSE feed subscriber needs and that no Slack mirror channel
  should retain. Aggregate-only collapses every credential read into
  ``{op_class, result_status}`` — enough for "someone touched a
  credential at 14:23", not enough to reconstruct what.
* ``audit_query`` — the request filter is the most damaging thing to
  broadcast: it encodes whoever the querying operator was investigating
  and on what hunch. The response payload also carries the raw audit
  rows the query matched, which inherits every other op's sensitivity.
  Broadcasting only ``{op_class, result_status, row_count}`` keeps the
  team-coordination signal ("X just queried audit") without leaking
  the investigation target or the matched evidence.

References
----------

* Decision #3 — ``docs/planning/v0.2-decisions.md``.
* MCP audit shape (G0.5-T5, the in-tree precedent for similar
  redaction discipline) —
  :func:`meho_backplane.mcp.audit.write_mcp_audit_row`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy

__all__ = [
    "BroadcastEvent",
    "classify_op",
    "redact_payload",
    "scrub_broadcast_params",
]


#: Op-ids that classify as ``credential_read``. Extensible — every
#: future credential-access verb (e.g. ``vault.transit.decrypt``,
#: ``secretsmanager.get``, ``onepassword.read``) gets added here when
#: it lands. Membership is the canonical signal; ``vault.kv.``-prefix
#: matching would over-match a hypothetical future ``vault.kv.stats``
#: which doesn't read secret content.
_CREDENTIAL_READ_OPS: Final[frozenset[str]] = frozenset(
    {
        "vault.kv.read",
        "vault.kv.list",
        # SDDC Manager is the system of record for nested-infra credentials;
        # its typed ``GET /v1/credentials`` read returns live account secrets
        # (#2306). Classified credential_read so audit + broadcast payloads
        # collapse to aggregate-only, on top of the op's requires_approval
        # gate and the connector-boundary secret scrub.
        "sddc.credential.list",
    }
)

#: Op-ids that classify as ``credential_mint``. Like
#: :data:`_CREDENTIAL_READ_OPS`, the explicit allowlist comes before
#: the write-suffix check so ``harbor.robot.create`` (which ends with
#: ``.create``) isn't misclassified as plain ``write``. Every op that
#: returns a freshly-minted secret credential in its response payload
#: belongs here — the broadcast collapses to aggregate-only so the
#: secret never reaches the SSE stream or any Slack mirror channel.
_CREDENTIAL_MINT_OPS: Final[frozenset[str]] = frozenset(
    {
        "harbor.robot.create",
        # G11.7-T1 #1401 — Vault auth ops whose *response* carries a
        # freshly-minted secret. ``vault.token.create`` returns a client
        # token; ``vault.auth.approle.generate_secret_id`` returns a
        # SecretID. Both must collapse to aggregate-only so the minted
        # credential never reaches the SSE stream or a Slack mirror. The
        # ``OperationResult`` returned to the caller still carries it.
        "vault.token.create",
        "vault.auth.approle.generate_secret_id",
        # G-Node/RKE2-T2 #2429 — the RKE2 server-token rotate mints a new
        # join token server-side. The handler never returns the token value
        # (only a Vault pointer), so nothing sensitive is in the response —
        # but ``.rotate`` would otherwise classify ``other`` and broadcast
        # full params/detail. Pinning to ``credential_mint`` collapses the
        # broadcast to aggregate-only as defence-in-depth for the op class.
        "rke2.token.rotate",
    }
)

#: Op-ids that classify as ``credential_write`` (G11.7-T1 #1401). Unlike
#: :data:`_CREDENTIAL_MINT_OPS` (secret in the *response*), these ops
#: carry the secret in their *request params* — the broadcast publisher
#: ships ``params`` (request-side), so a plain ``write`` classification
#: would leak the written credential to every operator on the feed.
#: Collapsing them to aggregate-only keeps the team-coordination signal
#: ("someone wrote a credential at 14:23") without the secret material.
#: The explicit allowlist comes before the ``.write`` / ``.create`` /
#: ``.update`` suffix branch so these win over the plain ``write`` class.
#:
#: * ``vault.auth.userpass.write`` / ``vault.auth.userpass.update_password``
#:   — the userpass password is in ``params``.
#: * ``vault.kv.put`` — the KV-v2 secret ``data`` is in ``params`` (this
#:   op shipped pre-G11.7 classified as plain ``write``, which broadcast
#:   the written secret in full; reclassifying it here closes that latent
#:   leak — see ``docs/codebase/connectors-vault.md``).
#: * ``vault.kv.patch`` — same posture as ``vault.kv.put``: the merged
#:   fields ride in ``params`` (G3.15-T1 #1409). Without the explicit
#:   pin the ``.patch`` write-suffix would classify it plain ``write``
#:   and broadcast the partial secret in full.
#: * ``k8s.secret.create`` — the Secret ``data`` / ``stringData`` is in
#:   ``params``.
#: * ``k8s.job.create`` — the Job ``spec`` carries a pod template whose
#:   inline ``env`` entries can hold credential material in ``params``
#:   (G3.14-T1 #1403).
_CREDENTIAL_WRITE_OPS: Final[frozenset[str]] = frozenset(
    {
        "vault.auth.userpass.write",
        "vault.auth.userpass.update_password",
        "vault.kv.put",
        "vault.kv.patch",
        "k8s.secret.create",
        "k8s.job.create",
        # G3.13-T4 #1406 — Keycloak user-credential write ops. Both source
        # the password from Vault (it is never an inline param), so the
        # request params carry only a Vault *path*. The pin is
        # defence-in-depth: should a future param-shape change ever place
        # credential material in params, the broadcast collapses to
        # aggregate-only rather than shipping it on the feed.
        "keycloak.user.create",
        "keycloak.user.reset_password",
    }
)

#: Op-id suffixes that imply mutation. Append to this tuple when a new
#: write-shaped verb spelling lands. ``.put`` is the KV-v2 write verb
#: (``vault.kv.put`` — G3.3-T1 #545); without it a secret write would
#: fall through to ``other`` and broadcast its full params, leaking the
#: written secret payload to every operator. Order doesn't matter —
#: :meth:`str.endswith` accepts a tuple and short-circuits on the
#: first match.
_WRITE_SUFFIXES: Final[tuple[str, ...]] = (
    ".create",
    ".update",
    ".delete",
    ".patch",
    ".put",
    # ``.write`` is the Vault mutating-write verb spelling for the auth
    # and sys-policy surfaces (G3.15 #1410/#1411): ``vault.auth.approle.write``
    # creates an AppRole (no secret in params → plain ``write``),
    # ``vault.sys.policy.write`` writes a policy document. Without this
    # suffix they fall through to ``other`` and broadcast their full
    # params. The secret-bearing ``.write`` ops
    # (``vault.auth.userpass.write`` / ``vault.auth.userpass.update_password``)
    # are pinned in :data:`_CREDENTIAL_WRITE_OPS`, which ``classify_op``
    # consults BEFORE this suffix branch, so this addition never downgrades
    # a credential write to plain ``write``.
    ".write",
    # bind9 record-write verbs (G3.4-T3 #589). The bind9 connector
    # uses ``.add`` / ``.remove`` rather than ``.create`` / ``.delete``
    # to match the consumer wrapper's verb shape (``--add-a-record``
    # / ``--remove-record``). Without these suffixes ``classify_op``
    # would fall through to ``other`` and the broadcast classifier
    # would emit the full param dict (including the rdata) as a
    # ``other``-class event rather than redact under the ``write``
    # branch.
    ".add",
    ".remove",
    # keycloak role-mapping privilege-grant verb (G3.13-T4 #1406).
    # ``keycloak.role_mapping.assign`` is a mutating privilege grant; its
    # params (user id/username + role names) carry no secret, so the plain
    # ``write`` class (full detail) is correct — but without this suffix it
    # falls through to ``other``. Classifying it ``write`` keeps the feed's
    # mutation signal accurate.
    ".assign",
)

#: Op-id suffixes that imply non-mutating read. ``.ls`` and ``.about``
#: are the CLI-shaped verbs (``meho vsphere ls``, ``meho meho about``)
#: that the connector layer maps to the same read class as ``.list`` /
#: ``.get`` / ``.info``. ``.health`` and ``.seal_status`` are the Vault
#: ``sys`` diagnostics verbs (G3.3-T2 #546): non-mutating cluster-state
#: reads with no secret content, so they broadcast at the same
#: ``read`` sensitivity as ``.list`` rather than falling through to
#: the full-detail ``other`` class. ``.versions`` is the KV-v2
#: version-metadata browse (``vault.kv.versions`` — G3.3-T1 #545): a
#: read of metadata only (no secret values), so it likewise classifies
#: ``read`` rather than ``credential_read``.
_READ_SUFFIXES: Final[tuple[str, ...]] = (
    ".list",
    ".info",
    ".get",
    ".about",
    ".ls",
    ".health",
    ".seal_status",
    ".versions",
)


class BroadcastEvent(BaseModel):
    """One broadcast event — exactly one per audited operation.

    The publish-on-write hook (T3) constructs an instance per audit-log
    write; ``XADD meho:feed:{tenant_id}`` carries it onto the per-tenant
    Valkey stream. SSE subscribers (T4) and MCP resource readers (T6)
    deserialise back to this shape.

    The model is **frozen** (``ConfigDict(frozen=True)``) — attribute
    reassignment (``event.payload = new_dict``) raises ``ValidationError``,
    mirroring the chassis
    :class:`~meho_backplane.auth.operator.Operator` pattern.
    ``frozen=True`` is **faux-immutability** in pydantic v2: nested
    mutable values like the ``payload`` dict CAN still be mutated in
    place — ``event.payload["k"] = "v"`` succeeds silently because it
    mutates the inner dict object rather than reassigning the
    attribute. Downstream consumers MUST NOT mutate ``payload``
    between construction and publish. The PII contract is enforced
    upstream by the publisher calling :func:`redact_payload` *before*
    construction, not by the model itself. A future tightening to
    ``Mapping[str, Any]`` + ``types.MappingProxyType`` would close
    the in-place-mutation door but requires a coordinated change to
    T3 / T4 / T6 deserialisation and is out of scope for T2.

    ``payload`` is **always** the redacted view per
    :func:`redact_payload` — callers MUST NOT pass raw params here.
    The class can't enforce this from the type system alone (any
    ``dict[str, Any]`` satisfies the annotation), so the contract is
    documented in the publisher's docstring (T3) and verified by
    integration tests that read back from the stream and assert
    forbidden keys are absent.
    """

    model_config = ConfigDict(frozen=True)

    #: G0.16-T6 Finding F (#1312) discriminator per
    #: ``docs/codebase/api-shape-conventions.md`` §6. Every entry on
    #: ``meho:feed:{tenant_id}`` carries a top-level ``kind`` field that
    #: consumers switch on:
    #:
    #: * ``"operation"`` -- audit-driven :class:`BroadcastEvent` (this
    #:   class), one per audited operation.
    #: * ``"agent_announcement"`` -- agent-authored
    #:   :class:`~meho_backplane.broadcast.agent_events.AgentAnnouncementEvent`,
    #:   published via ``meho.broadcast.announce``.
    #:
    #: Default value, not :class:`typing.Literal`-pinned, so the
    #: history-parser's switch-on-``kind`` covers two cases under a
    #: single read path: (a) post-migration entries XADD'd by the
    #: current publisher (``kind: "operation"`` written on the wire),
    #: and (b) pre-migration entries written before this field
    #: existed -- those entries lack ``kind`` on the wire, and the
    #: parser infers ``"operation"`` from the absence of
    #: ``"agent_announcement"``-shape fields. The historical
    #: ``event_kind`` field on the sibling agent-announcement class
    #: stays accepted on read as a backward-compatible alias.
    kind: str = "operation"

    event_id: UUID
    ts: datetime
    tenant_id: UUID
    principal_sub: str
    #: Best-effort from the JWT's ``name`` claim cached at audit-write
    #: time. Many JWTs ship without a ``name`` claim; the publisher
    #: must be able to omit this field without forcing every call site
    #: to pass an explicit ``None``. Same reasoning for ``target_name``.
    principal_name: str | None = None
    target_name: str | None = None
    op_id: str
    #: One of ``"read"`` / ``"write"`` / ``"credential_read"`` /
    #: ``"credential_mint"`` / ``"credential_write"`` / ``"audit_query"``
    #: / ``"other"``. Derived from :func:`classify_op` at publish time.
    op_class: str
    #: One of ``"ok"`` / ``"error"`` / ``"denied"``. The handler
    #: produces it; the broadcast publisher does not re-classify.
    result_status: str
    #: FK to ``audit_log.id``. The broadcast event is downstream of the
    #: audit write — audit is the canonical record, broadcast is the
    #: real-time view. A subscriber that wants the full untruncated row
    #: queries audit_log by this id.
    audit_id: UUID
    #: Redacted view per :func:`redact_payload`. NEVER raw params for
    #: ``credential_read``, ``credential_mint``, ``credential_write``, or
    #: ``audit_query`` classes — the redaction contract is upstream of
    #: this field.
    payload: dict[str, Any] = Field(default_factory=dict)


# code-quality-allow: flat order-significant op-id classifier; length is the
# explanatory match-order docstring, not branching. Pre-existing >100 (126 on
# main); #2406 only adds the `net.` prefix to an existing read tuple.
def classify_op(op_id: str) -> str:
    """Map an op-id to one of the sensitivity classes.

    Match order is policy-significant:

    1. ``credential_read`` — explicit allowlist first so a hypothetical
       future ``vault.kv.audit-list`` (audit metadata, not secret
       content) could opt out by being absent from
       :data:`_CREDENTIAL_READ_OPS` and falling through to the suffix
       check.
    2. ``credential_mint`` — explicit allowlist for ops that return a
       freshly-minted secret in their response payload (e.g.
       ``harbor.robot.create``, ``vault.token.create``). Checked before
       the ``.create`` suffix so the allowlist wins over the
       mutation-suffix branch.
    3. ``credential_write`` — explicit allowlist for write ops whose
       *request params* carry a secret (e.g.
       ``vault.auth.userpass.write``, ``vault.kv.put``,
       ``k8s.secret.create``). Checked before the write-suffix branch
       so the secret-bearing params collapse to aggregate-only instead
       of broadcasting in full under the plain ``write`` class
       (G11.7-T1 #1401).
    4. ``audit_query`` — every op-id with the ``audit.`` or
       ``meho.audit.`` prefix classifies as audit_query regardless of
       the verb suffix. The ``meho.audit.`` arm catches the admin
       replay meta-tool (``meho.audit.replay``, G8.2-T6 #1014): the
       MCP broadcast path classifies via ``classify_op(op_id)`` with
       the tool name verbatim, so without this arm the replay tool
       would fall through to ``other`` and broadcast its full
       ``ReplayNode`` payload instead of the aggregate-only view.
    4b. ``approval`` — every op-id with the ``approval.`` prefix
       (``approval.pending`` / ``.approved`` / ``.rejected`` /
       ``.expired``, emitted by
       :func:`~meho_backplane.operations.approval_queue.publish_approval_event`)
       classifies as the dedicated ``approval`` class. The approvals
       console surface (G10.7-T3 #1778) subscribes its notifications
       bell to ``/ui/broadcast/stream?op_class=approval`` -- an
       exact-match server-side filter on this class -- so the lifecycle
       events need their own class rather than falling through to
       ``other`` (which would mix them with every unclassified op and
       defeat the filter). The class is **not** sensitive
       (``approval.*`` payloads carry only ``connector_id`` / ``op_id`` /
       ``decision`` / the request id, never secret material), so it
       stays full-detail like ``other`` did -- see
       :data:`~meho_backplane.broadcast.overrides._SENSITIVE_OP_CLASSES`.
    5. ``read`` / ``write`` — HTTP-method-prefixed ingested op IDs
       (e.g. ``GET:/api/v2.0/systeminfo``). ``GET:`` and ``HEAD:``
       map to ``read``; ``POST:``, ``PUT:``, ``PATCH:``, ``DELETE:``
       map to ``write``. Checked before the dot-suffix branches since
       ingested ops carry no meho verb suffix. The ``net.`` product
       prefix (the synthetic network-diagnostics connector, #2406)
       rides the ``read`` arm here: every ``net.*`` op is a
       non-mutating probe, matched by prefix because its verbs are
       underscore-joined (``net.tcp_check``), so a dotted read suffix
       never matches.
    6. ``write`` — mutation suffixes (``.create`` / ``.update`` /
       ``.delete`` / ``.patch`` / ``.put`` / ``.write`` / ``.add`` /
       ``.remove``). The ``_CREDENTIAL_WRITE_OPS`` allowlist (step 3)
       runs first, so a ``.write``-shaped secret-bearing op like
       ``vault.auth.userpass.write`` keeps its ``credential_write``
       class.
    7. ``read`` — non-mutating verb suffixes (``.list`` / ``.info`` /
       ``.get`` / ``.about`` / ``.ls`` / ``.health`` / ``.seal_status``
       / ``.versions``). ``.read`` is deliberately **not** a read
       suffix: it would over-match the ``credential_read``-allowlisted
       ``vault.kv.read`` (the allowlist wins, but the exclusion keeps
       the policy single-sourced) and would reclassify the auth-config
       ``.read`` ops that intentionally broadcast as ``other``.
    8. ``other`` — everything else. Falls through to full-detail
       broadcast per decision #3.

    Examples
    --------

    >>> classify_op("vault.kv.read")
    'credential_read'
    >>> classify_op("harbor.robot.create")
    'credential_mint'
    >>> classify_op("vault.token.create")
    'credential_mint'
    >>> classify_op("vault.auth.userpass.write")
    'credential_write'
    >>> classify_op("vault.kv.put")
    'credential_write'
    >>> classify_op("audit.query")
    'audit_query'
    >>> classify_op("meho.audit.replay")
    'audit_query'
    >>> classify_op("approval.pending")
    'approval'
    >>> classify_op("approval.approved")
    'approval'
    >>> classify_op("GET:/api/v2.0/systeminfo")
    'read'
    >>> classify_op("DELETE:/api/v2.0/projects/myproj/repositories/myrepo")
    'write'
    >>> classify_op("net.tcp_check")
    'read'
    >>> classify_op("vsphere.vm.list")
    'read'
    >>> classify_op("vsphere.vm.create")
    'write'
    >>> classify_op("some.unknown.op")
    'other'
    """
    if op_id in _CREDENTIAL_READ_OPS:
        return "credential_read"
    if op_id in _CREDENTIAL_MINT_OPS:
        return "credential_mint"
    if op_id in _CREDENTIAL_WRITE_OPS:
        return "credential_write"
    if op_id.startswith(("audit.", "meho.audit.")):
        return "audit_query"
    # Approval-queue lifecycle events (approval.pending / .approved /
    # .rejected / .expired). Their own class so the approvals console
    # bell can filter the SSE bridge to op_class=approval (#1778);
    # not sensitive, so it stays full-detail like the `other` fall-through.
    if op_id.startswith("approval."):
        return "approval"
    # Ingested ops use HTTP-method prefixes (e.g. "GET:/api/v2.0/systeminfo");
    # GET/HEAD are safe reads by HTTP semantics. The synthetic net.* probes
    # (#2406) ride the same arm: every net.* op is a non-mutating
    # reachability probe, and it's matched by product prefix (not verb
    # suffix) because the verbs are underscore-joined (`net.tcp_check`), so
    # a dotted read suffix like `.check` never matches the `tcp_check`
    # segment. Checked after the explicit allowlists so credential_mint pins
    # still win.
    if op_id.startswith(("GET:", "HEAD:", "net.")):
        return "read"
    if op_id.startswith(("POST:", "PUT:", "PATCH:", "DELETE:")):
        return "write"
    if op_id.endswith(_WRITE_SUFFIXES):
        return "write"
    if op_id.endswith(_READ_SUFFIXES):
        return "read"
    return "other"


def _maybe_row_count(raw_params: dict[str, Any]) -> int | None:
    """Extract ``row_count`` from the publisher's combined params dict.

    The publish-on-write hook (T3) merges request params and response
    summary into one dict before calling :func:`redact_payload`. For
    ``audit_query`` ops the response carries a ``row_count`` field
    (per the G8 audit-query API in #334); for older or non-conforming
    callers it may be absent.

    Returns ``None`` rather than ``0`` when the field is missing so
    subscribers can distinguish "the query matched zero rows" from
    "the publisher didn't surface a count". Coerces to ``int`` defensively
    — a stringified count from a JSON round-trip would otherwise serialise
    back as a string.
    """
    raw = raw_params.get("row_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def redact_payload(
    op_class: str,
    raw_params: dict[str, Any],
    result_status: str,
    *,
    detail: Literal["full", "aggregate"] | None = None,
) -> dict[str, Any]:
    """Return the broadcast-safe payload view for *op_class*.

    Three shapes -- selected by the *effective* detail rather than the
    op_class alone:

    * **Aggregate**, sensitive class (``audit_query``) →
      ``{op_class, result_status, row_count}``. The audit-query
      aggregate retains the response row-count -- a useful
      team-coordination signal -- but never the filter or matched rows.
    * **Aggregate**, any other class →
      ``{op_class, result_status}``. The same shape G6.1's
      ``credential_read`` default used; pulled out as the universal
      aggregate when a tenant rule downgrades a normally-full op
      (e.g. ``k8s.configmap.info`` scoped to ``kube-system``).
    * **Full** →
      ``{op_class, params=raw_params, result_status}``. Full request
      detail; nested objects pass through verbatim. Used by the
      everything-else default and also by the G6.3 ``request_override``
      branch that upgrades a sensitive class to full detail per
      operator opt-in.

    *detail* is the G6.3-T2 (#379) extension. When ``None`` (the
    pre-G6.3 default), the function falls back to decision #3 of
    ``docs/planning/v0.2-decisions.md`` -- aggregate for sensitive
    classes (``credential_read`` / ``credential_mint`` /
    ``credential_write`` / ``audit_query``), full for everything else.
    When ``"aggregate"`` or ``"full"`` is passed
    explicitly, the resolver (:func:`compute_effective_broadcast_detail`)
    has already decided the effective detail and this function just
    renders it. Callers that don't go through the resolver
    (:func:`meho_backplane.operations._audit.publish_broadcast`) keep
    the ``detail=None`` default and the existing per-class behaviour.

    Forward-compatibility note: the return shape is a plain ``dict``
    rather than a typed model because the downstream
    :attr:`BroadcastEvent.payload` field is already ``dict[str, Any]``.
    Promoting either side to a structured model would require a
    coordinated change to all of T3 / T4 / T6 and is out of scope.
    """
    if detail is None:
        effective_detail: Literal["full", "aggregate"] = (
            "aggregate"
            if op_class in {"credential_read", "credential_mint", "credential_write", "audit_query"}
            else "full"
        )
    else:
        effective_detail = detail

    if effective_detail == "aggregate":
        if op_class == "audit_query":
            return {
                "op_class": op_class,
                "result_status": result_status,
                "row_count": _maybe_row_count(raw_params),
            }
        return {"op_class": op_class, "result_status": result_status}

    return {
        "op_class": op_class,
        "params": raw_params,
        "result_status": result_status,
    }


# ---------------------------------------------------------------------------
# Tier-1 defence-in-depth for full-detail broadcasts (meho-internal #151)
# ---------------------------------------------------------------------------
#
# ``classify_op`` is allowlist-driven: whether a secret-bearing write op
# collapses to aggregate-only depends on a human having pinned it into
# :data:`_CREDENTIAL_WRITE_OPS` / :data:`_CREDENTIAL_MINT_OPS`. A newly
# ingested op, a mis-registered op, or a connector op added without a
# pin falls through to the ``write`` / ``other`` class and would ship
# its full request params to every co-tenant feed subscriber. The
# helpers below are the classification-independent second layer: the
# dispatch publisher runs every params dict through
# :func:`scrub_broadcast_params` before building the payload, and
# collapses the broadcast to aggregate-only whenever secret material
# is detected. The static classifier-coverage test
# (``tests/test_broadcast_classifier_coverage.py``) is the companion
# CI layer that forces registered secret-bearing write ops to get
# pinned; this runtime layer covers everything the static sweep cannot
# see.

#: Name-tokens that mark a param key as secret-bearing wherever they
#: appear in the normalised (snake-cased) token list: ``password`` /
#: ``user_password`` / ``passphrase`` / ``kubeconfig`` are secret
#: material in any position. ``key`` alone is deliberately NOT here
#: (``ssh_public_key`` / ``label_key`` would over-match) -- key-ish
#: names are covered by the pair combinations below.
_SECRET_NAME_TOKENS_ANYWHERE: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "apikey",
        "kubeconfig",
    }
)

#: Name-tokens that mark a key as secret-bearing only in **final**
#: position. ``token`` / ``secret`` name the credential itself when
#: final (``token``, ``session_token``, ``client_secret``,
#: ``credentials``) but an *attribute or reference* of it when
#: followed by more tokens (``token_policies`` / ``token_ttl`` on the
#: Vault AppRole write surface, ``secret_name`` / ``secret_path`` as
#: k8s/Vault references) -- those carry no secret material and must
#: not collapse a vetted full-detail write broadcast to aggregate.
_SECRET_NAME_TOKENS_FINAL: Final[frozenset[str]] = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
    }
)

#: Adjacent token pairs that mark a key as secret-bearing when the
#: positional rules above don't (``api_key``, ``private_key``,
#: ``secret_id`` -- the Vault AppRole SecretID is secret material
#: even though ``secret`` is non-final there).
_SECRET_NAME_TOKEN_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("encryption", "key"),
        ("secret", "id"),
        ("secret", "value"),
        ("secret", "data"),
    }
)

#: Replacement value for a secret-named param. Mirrors the Tier-1
#: engine's ``[REDACTED:<pattern>]`` shape so feed consumers see one
#: consistent redaction vocabulary.
_REDACTED_PARAM: Final[str] = "[REDACTED:param_name]"

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _is_secret_param_name(name: str) -> bool:
    """True when *name* is a secret-shaped param/field name.

    Normalises camelCase and kebab-case to snake_case tokens, then
    checks single tokens and adjacent pairs. Shared by the runtime
    scrub below and the classifier-coverage test's parameter-schema
    sweep so both layers agree on what "secret-bearing" means.
    """
    normalised = _CAMEL_BOUNDARY.sub("_", name).replace("-", "_").replace(".", "_").lower()
    tokens = [token for token in normalised.split("_") if token]
    if not tokens:
        return False
    if any(token in _SECRET_NAME_TOKENS_ANYWHERE for token in tokens):
        return True
    if tokens[-1] in _SECRET_NAME_TOKENS_FINAL:
        return True
    return any(pair in _SECRET_NAME_TOKEN_PAIRS for pair in itertools.pairwise(tokens))


def _scrub_secret_named_keys(node: Any) -> tuple[Any, bool]:
    """Replace values of secret-named keys; report whether any were found.

    The Tier-1 engine matches *labelled* secret shapes inside string
    leaves (``password=...``, ``Bearer ...``); a dict entry like
    ``{"password": "hunter2"}`` carries no label in the leaf itself, so
    the engine alone would pass it through verbatim. This walk is the
    key-name-aware complement: any mapping entry whose key is
    secret-shaped has its whole value subtree replaced (a nested value
    under a ``credentials`` key is secret material regardless of its
    inner structure).

    Bare scalars (bool / int / float / ``None``) under a secret-shaped
    key pass through unflagged: secret material is a string or a blob,
    never ``bind_secret_id=true`` or ``secret_id_ttl=3600`` -- the
    Vault AppRole config attributes that would otherwise collapse a
    vetted full-detail write broadcast to aggregate on every dispatch.
    """
    if isinstance(node, Mapping):
        found = False
        scrubbed: dict[str, Any] = {}
        for key, value in node.items():
            if _is_secret_param_name(str(key)) and isinstance(value, (str, Mapping, Sequence)):
                scrubbed[str(key)] = _REDACTED_PARAM
                found = True
                continue
            child, child_found = _scrub_secret_named_keys(value)
            scrubbed[str(key)] = child
            found = found or child_found
        return scrubbed, found
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        items: list[Any] = []
        found = False
        for item in node:
            child, child_found = _scrub_secret_named_keys(item)
            items.append(child)
            found = found or child_found
        return items, found
    return node, False


def scrub_broadcast_params(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Tier-1-scrub a broadcast params dict; report secret detection.

    Returns ``(scrubbed_params, secret_detected)``. Two passes:

    1. Key-name scrub (:func:`_scrub_secret_named_keys`) -- replaces the
       value of every secret-named key (``password``, ``client_secret``,
       ``sessionToken``, ...).
    2. Tier-1 engine pass with the packaged default policy -- catches
       labelled secret shapes embedded in string values (``Bearer ...``,
       ``api_key=...``, ``Authorization:`` header dumps). The policy is
       pinned via :func:`~meho_backplane.redaction.resolver.get_default_policy`,
       deliberately bypassing the resolver's override table: an
       operator-registered shadow-mode or narrowed policy must not
       silently disable the broadcast floor.

    ``secret_detected`` is ``True`` when either pass found secret
    material; the dispatch publisher uses it to collapse the broadcast
    to aggregate-only, so a mis/unclassified secret-bearing op never
    ships even a redacted-shape params dict. Fail-closed: any exception
    reports ``({}, True)`` -- on this fail-open publish path an
    unredacted fallback would be the leak.
    """
    try:
        scrubbed, found = _scrub_secret_named_keys(params)
        result = redact(scrubbed, get_default_policy())
        found = found or bool(result.manifest)
        redacted = result.redacted
        if not isinstance(redacted, dict):
            return {}, True
        return redacted, found
    except Exception:  # fail-closed: never leak, never raise on the publish path
        return {}, True
