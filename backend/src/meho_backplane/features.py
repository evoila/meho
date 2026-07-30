# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Deploy-time feature-gate visibility for ``/ready``.

v0.6.0 ships four major features that each require additional deploy
configuration not flagged anywhere operator-facing
(``claude-rdc-hetzner-dc#697`` signals 16, 17). The pattern is the same
across all four: the feature's code ships, env vars get added in
:mod:`meho_backplane.settings`, but no ``/ready`` check, no
``docs/RELEASING.md`` callout, no release-body "what won't work out of
the box" section. Operators discover the dependency by hitting the
surface and reading the 503 — or worse, the silent NULL column (audit
replay before T6's fix).

This module exposes the gates as one structured block on ``/ready`` so
an operator's single GET answers: "which features will work out of the
box on my deploy?". Each entry carries:

* ``configured`` — bool. True when every env var the feature requires
  is non-empty.
* ``missing_env`` — list[str]. Names of the env vars the operator still
  needs to set. Empty when ``configured`` is True. Names match the
  ``backend/src/meho_backplane/settings.py`` env-var contract; reading
  the corresponding settings.py field docstring tells the operator
  what the value is for.
* ``docs`` — relative path inside the repo / rendered docs site
  explaining how to provision the missing pieces. Present
  unconditionally on ``agent_runtime`` and ``ui_surface`` (configured
  and unconfigured alike, so the operator looking at the happy-path
  surface still has the provenance trail to the doc that describes
  the setup). Absent on ``audit_replay`` (capture is feature-coupled
  to MCP itself, not to an admin-configurable knob — operators have
  no separate setup doc to read), on the ``mcp`` block (the pinned
  ``protocol_version`` is a build-time constant, not a deploy-time
  knob), and on the transitive ``approval_queue`` entry (which
  surfaces a ``depends_on`` field pointing at ``agent_runtime``
  instead).

The audit-replay entry additionally exposes ``capture_mode`` —
``"enforced"`` until G0.14-T6 (#1147) decouples capture from
enforcement, ``"always"`` after. The shape is forward-compatible: T6
flips this from ``"enforced"`` to ``"always"`` and adds nothing else.

Feature-maturity registry (#2674)
---------------------------------

This module is also the **single source of truth for feature
maturity** (#2664): :data:`FEATURE_MATURITY` maps every user-facing
MEHO feature to its tier — ``ga`` / ``beta`` / ``experimental`` — plus,
on non-GA entries, the ``target_ga`` milestone and the ``tracking``
issue URL. Every maturity-labelled surface derives from this one dict:

* ``/ready`` — the deploy-gate entries this module already emits merge
  the maturity fields in (see :data:`_READY_ENTRY_FEATURE`).
* MCP tool-description prefixes + ``initialize.instructions`` summary
  and the OpenAPI ``x-maturity`` extension (#2675).
* The CLI command-manifest ``maturity`` field (#2676).
* The ``/ui`` area-header badge chips (#2677).
* The generated docs maturity-index page + CI drift guard (#2678).

Those consumers **import this module** (server-side render, tool
registration, docs build) — deliberately no dedicated REST read
surface: the smallest thing that serves every #2664 consumer is the
module itself, and ``/ready`` already carries the merged view for
operator tooling.

Reclassifying a feature (the v0.28 post-eval round, Goal #2661) is a
one-line data edit in :data:`FEATURE_MATURITY`; no surface-specific
edits anywhere. The classification encoded here is the **provisional**
one tabled in #2664, pending clean-room eval round 1 (#2665). Tier
semantics (#2664 entry criteria): ``ga`` features carry the 1.0
stability promise; ``beta`` works end-to-end somewhere real with known,
tracked gaps and may change with deprecation notice; ``experimental``
may change or vanish and sits outside the 1.0 promise.

The module is **pure** — it reads :class:`~meho_backplane.settings.Settings`
and returns a plain :class:`dict`. No I/O, no settings cache mutation,
no environment lookup. Callers that need to test against a synthetic
configuration build a :class:`Settings` instance with the desired
attribute values and pass it in; production paths read
:func:`~meho_backplane.settings.get_settings` once at request time.
This is the same shape :mod:`meho_backplane.api.v1.auth_config` uses
for its discovery payload.

References
----------

* Consumer feedback (canonical): ``claude-rdc-hetzner-dc#697`` signals
  16 + 17 — the original "expose feature-gate state" flag.
* Convention anchor: :doc:`docs/codebase/error-message-shape.md`
  (G0.14-T11) — the missing-env messages here mirror the convention's
  diagnostic-values + remediation + doc-reference shape.
* Sibling: G0.14-T6 (#1147) flips ``audit_replay.capture_mode`` from
  ``"enforced"`` to ``"always"`` once it lands.
* Maturity program: #2664 (initiative — classification table + entry
  criteria), #2674 (this registry), #2675-#2678 (surface propagation),
  Goal #2661 milestone plan (v0.28 reclassification, v1.0.0 GA set).
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from meho_backplane.settings import Settings

__all__ = [
    "FEATURE_MATURITY",
    "Maturity",
    "MaturityInfo",
    "build_features_block",
]

Maturity = Literal["ga", "beta", "experimental"]

_TRACKER = "https://github.com/evoila/meho/issues"


class MaturityInfo(TypedDict):
    """One :data:`FEATURE_MATURITY` entry.

    ``target_ga`` and ``tracking`` are present on non-GA entries only
    (mirroring how ``docs`` is present only where a setup doc exists).
    ``target_ga`` may be ``None`` on ``experimental`` entries: those sit
    outside the 1.0 promise (#2664 entry criteria; several are explicit
    1.0 non-goals in #2661), so advertising a committed milestone would
    overstate exactly the way this program exists to prevent.
    """

    maturity: Maturity
    target_ga: NotRequired[str | None]
    tracking: NotRequired[str]


#: The provisional #2664 classification. Keys are the canonical feature
#: identifiers every surface (#2675-#2678) maps its tools / routes /
#: commands / UI areas onto. Renaming a key is a cross-surface break;
#: retiering one is a one-line edit here and nowhere else.
#:
#: ``tracking`` points at the open issue where the feature's road to
#: promotion is visible today: the concrete gap issue where one exists
#: (#2668 background-execution credential identity for sensors and the
#: scheduler, #2667 first-class GSM chart values, #2656 the v0.25.0
#: hosted-agent-run hardening line), the clean-room eval (#2665) for
#: beta features whose promotion gate is the eval score itself, and the
#: 1.0 Goal (#2661, which schedules the v0.28 re-triage) for
#: experimental features with no committed path.
FEATURE_MATURITY: dict[str, MaturityInfo] = {
    # GA-track (provisional): the read/governance/knowledge plane the
    # 2026-07-21 grounding review found provably working in the field.
    "audit": {"maturity": "ga"},
    "memory_knowledge": {"maturity": "ga"},
    "targets": {"maturity": "ga"},
    "typed_connector_reads": {"maturity": "ga"},
    "net_diagnostics": {"maturity": "ga"},
    "approvals": {"maturity": "ga"},
    "auth_tenancy": {"maturity": "ga"},
    # Beta: works end-to-end somewhere real; gaps known and tracked.
    "sensors": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2668",
    },
    "topology": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2665",
    },
    "broadcast": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2665",
    },
    "scheduler": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2668",
    },
    "ui_console": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2665",
    },
    "write_surfaces": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2665",
    },
    "gsm_backend": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2667",
    },
    "satellite_gateway": {
        "maturity": "beta",
        "target_ga": "v1.0.0",
        "tracking": f"{_TRACKER}/2665",
    },
    # Experimental: may change or vanish; outside the 1.0 promise.
    "connector_ingest": {
        "maturity": "experimental",
        "target_ga": None,
        "tracking": f"{_TRACKER}/2661",
    },
    "agent_runtime": {
        "maturity": "experimental",
        "target_ga": None,
        "tracking": f"{_TRACKER}/2656",
    },
    "doc_collections": {
        "maturity": "experimental",
        "target_ga": None,
        "tracking": f"{_TRACKER}/2661",
    },
    "two_world_ops": {
        "maturity": "experimental",
        "target_ga": None,
        "tracking": f"{_TRACKER}/2661",
    },
}

#: Which ``/ready`` entry maps to which :data:`FEATURE_MATURITY` key.
#: The block keys are deploy-gate names (what needs wiring); the
#: registry keys are feature names (what carries a promise) — they
#: coincide only for ``agent_runtime``. The ``mcp`` entry is absent by
#: design: it reports a build-time protocol constant, not a classified
#: feature — individual MCP tools inherit maturity from their owning
#: feature at registration time (#2675).
_READY_ENTRY_FEATURE: dict[str, str] = {
    "agent_runtime": "agent_runtime",
    "ui_surface": "ui_console",
    "audit_replay": "audit",
    "approval_queue": "approvals",
}


def _agent_runtime_block(settings: Settings) -> dict[str, Any]:
    """Whether the agent-principal lifecycle surface is operative.

    Configured iff all three :attr:`Settings.keycloak_admin_url`,
    :attr:`Settings.keycloak_admin_client_id`, and
    :attr:`Settings.keycloak_admin_client_secret` are non-empty. Any of
    the three unset surfaces as a 503 from
    ``POST /api/v1/agent-principals`` carrying the same env-var names;
    see :mod:`meho_backplane.api.v1.agent_principals` and
    :func:`meho_backplane.auth.keycloak_admin.KeycloakAdminClient.from_settings`.
    """
    missing: list[str] = []
    if not settings.keycloak_admin_url:
        missing.append("KEYCLOAK_ADMIN_URL")
    if not settings.keycloak_admin_client_id:
        missing.append("KEYCLOAK_ADMIN_CLIENT_ID")
    if not settings.keycloak_admin_client_secret:
        missing.append("KEYCLOAK_ADMIN_CLIENT_SECRET")
    return {
        "configured": not missing,
        "missing_env": missing,
        "docs": "docs/cross-repo/keycloak-agent-client.md",
    }


def _ui_surface_block(settings: Settings) -> dict[str, Any]:
    """Whether the operator-console BFF login flow is operative.

    Configured iff all three :attr:`Settings.ui_keycloak_client_id`,
    :attr:`Settings.ui_keycloak_client_secret`, and
    :attr:`Settings.ui_session_encryption_key` are non-empty. Any of
    the three unset breaks the flow at runtime:

    * ``UI_KEYCLOAK_CLIENT_ID`` / ``UI_KEYCLOAK_CLIENT_SECRET`` unset →
      503 from ``GET /ui/auth/login`` carrying the gold-standard
      :data:`~meho_backplane.ui.auth.flow.MISSING_CLIENT_SECRET_DETAIL`
      message.
    * ``UI_SESSION_ENCRYPTION_KEY`` unset →
      :class:`~meho_backplane.ui.auth.session_store.MissingSessionEncryptionKeyError`
      on the first session-cookie write attempt; the BFF cannot
      Fernet-encrypt the session payload without it. G0.15-T5 (#1214)
      added this third env var to ``missing_env`` so the ``/ready``
      self-doc enumerates the full set the operator must wire —
      previously the block advertised two, the operator set those,
      redeployed, and hit a 500 on first login.

    The doc reference matches the one the 503 cites and where all
    three env vars are documented under Check 3 of
    ``docs/cross-repo/keycloak-web-client.md``.
    """
    missing: list[str] = []
    if not settings.ui_keycloak_client_id:
        missing.append("UI_KEYCLOAK_CLIENT_ID")
    if not settings.ui_keycloak_client_secret:
        missing.append("UI_KEYCLOAK_CLIENT_SECRET")
    if not settings.ui_session_encryption_key:
        missing.append("UI_SESSION_ENCRYPTION_KEY")
    return {
        "configured": not missing,
        "missing_env": missing,
        "docs": "docs/cross-repo/keycloak-web-client.md",
    }


def _audit_replay_block() -> dict[str, Any]:
    """Whether MCP audit replay captures session ids.

    Today (pre-T6) the capture is gated by
    :attr:`Settings.mcp_require_session_id`: the session id is only
    written into the audit row when the operator has flipped
    ``MCP_REQUIRE_SESSION_ID=true`` (which also flips the surface
    behaviour: a missing header now returns ``-32600`` Invalid
    Request, instead of falling back to a single-call ``uuid4()``).
    This module surfaces ``capture_mode="enforced"`` to convey that
    "captured iff enforcement is on" coupling.

    G0.14-T6 (#1147) decouples capture from enforcement — once it
    lands, capture-if-present becomes unconditional, and
    ``capture_mode`` flips to ``"always"``. Operators reading the
    ``/ready`` payload before T6 lands see the coupling; readers
    after T6 lands see the post-decouple state. The schema is
    forward-compatible across the change: only the string value
    flips.

    No env var is "missing" for this feature — the capture is
    feature-coupled to MCP itself, not to an admin-configurable
    knob. ``missing_env`` is always ``[]`` and ``configured`` is
    always ``True``; the ``capture_mode`` field is the operative
    discriminant. The pre-T6 value of ``capture_mode`` is the
    constant ``"enforced"`` — not derived from
    :attr:`Settings.mcp_require_session_id` — because *capture* and
    *enforcement* are the same knob today, regardless of how the
    operator has it wired. T6 will flip this constant to
    ``"always"`` in a one-line edit.
    """
    return {
        "configured": True,
        "capture_mode": "enforced",
        "missing_env": [],
    }


def _mcp_block() -> dict[str, Any]:
    """MCP-layer runtime visibility for ``/ready``.

    Today the only field is ``protocol_version`` — the spec revision the
    server pins (currently :data:`~meho_backplane.mcp.schemas.PROTOCOL_VERSION`).
    The block exists so an unauthenticated deploy operator can answer
    "which MCP revision will this server negotiate with my clients?"
    from a single GET, without an authenticated ``/api/v1/health`` call.
    Mirrors the ``mcp_session_id_capture`` precedent (G0.14-T6 #1147):
    single-field operator visibility into the MCP layer's runtime
    state, with the matching field on
    :class:`~meho_backplane.api.v1.health.HealthResponse` so both
    surfaces stay consistent.

    No env var is "missing" — :data:`PROTOCOL_VERSION` is a build-time
    constant, not an admin-configurable knob; ``missing_env`` is
    therefore always ``[]`` and ``configured`` is always ``True``. The
    block does not carry a ``docs`` field for the same reason the
    ``audit_replay`` block doesn't: there is no deploy-time setup
    document an operator needs to read to "turn this on".

    G0.14-T13 (#1202) shipped this as the **observability** half of the
    MCP ``initialize`` mismatch handling — the behaviour half (refusing
    / down-negotiating / version-conditional capability advertisement)
    is explicit follow-up work, gated on demand evidence from real
    deployments. Operators reading this field can see which revision a
    given server pins; the matching
    ``mcp_initialize_protocol_version_mismatch`` WARNING log (emitted
    by :func:`~meho_backplane.mcp.server._initialize`) gives them the
    converse — which clients are pinned to a different revision.
    """
    # Function-local import to break the
    # ``features -> mcp.schemas -> mcp/__init__ -> mcp.handlers ->
    # broadcast -> health -> features`` cycle. ``health.py`` imports
    # ``build_features_block`` at module top-level (the ``/ready``
    # route hands the result to ``JSONResponse``), and ``mcp/__init__.py``
    # eagerly imports ``handlers`` to register the T3 JSON-RPC methods
    # — both can't sit above this module without re-entering it
    # during initial import. ``mcp.schemas`` itself is a leaf module
    # (no imports from elsewhere in the package), so reaching for it
    # function-locally is cheap and safe; the per-request cost of one
    # already-cached ``import`` is negligible against the rest of
    # ``/ready``'s probe-fanout work.
    from meho_backplane.mcp.schemas import PROTOCOL_VERSION

    return {
        "configured": True,
        "protocol_version": PROTOCOL_VERSION,
        "missing_env": [],
    }


def _approval_queue_block(settings: Settings) -> dict[str, Any]:
    """Whether the agent-grant approval queue is operative.

    The approval queue is transitive on the agent runtime: if the
    agent-principal surface is unreachable (Keycloak admin not
    configured), no agent has identity, so the approval queue has
    nothing to queue against. The block exposes
    ``depends_on="agent_runtime"`` instead of a separate
    ``missing_env`` so the operator's remediation chain is one step
    deep ("configure agent_runtime, the queue activates").

    ``effective_posture`` surfaces the resolved four-eyes stance
    (#2087): ``"four_eyes_enforced"`` when
    :attr:`Settings.approval_allow_self_approval` is ``False`` (the
    fail-closed default — requester != approver enforced by
    :func:`~meho_backplane.operations.approval_queue.approve_request`),
    ``"single_operator_break_glass"`` when the emergency
    ``APPROVAL_ALLOW_SELF_APPROVAL=true`` escape is live. One GET on
    ``/ready`` tells an operator (or auditor) whether the deploy is
    running with the break-glass flag set, without grepping the
    values file. The field reports posture, not health — it never
    affects ``configured``.
    """
    agent_runtime = _agent_runtime_block(settings)
    return {
        "configured": agent_runtime["configured"],
        "depends_on": "agent_runtime",
        "effective_posture": (
            "single_operator_break_glass"
            if settings.approval_allow_self_approval
            else "four_eyes_enforced"
        ),
    }


def build_features_block(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return the ``features`` block exposed under ``/ready``.

    Pure function over a :class:`Settings` snapshot — no env reads, no
    cache mutation, no I/O. Callers compose this into the ``/ready``
    payload directly; tests build a :class:`Settings` instance with
    the attribute values under test and assert against the returned
    dict.

    The five entries (``agent_runtime``, ``ui_surface``,
    ``audit_replay``, ``approval_queue``, ``mcp``) match the gated
    features the release ships plus the MCP-layer visibility block
    (G0.14-T13 #1202). The block is **closed** by design: adding a new
    gated feature is an additive change to this function and the
    corresponding entry in the ``docs/RELEASING.md`` post-deploy
    enablement section; renaming one is a wire-compat break for
    operator tooling that reads ``/ready``.

    Every entry with a :data:`_READY_ENTRY_FEATURE` mapping additionally
    carries the merged :data:`FEATURE_MATURITY` fields (#2674):
    ``maturity`` always, plus ``target_ga`` / ``tracking`` on non-GA
    features. The ``mcp`` entry carries none — see
    :data:`_READY_ENTRY_FEATURE` for why. Additive only: operator
    tooling keyed on the pre-#2674 fields is unaffected.

    Shape (verified by the issue body):

    .. code-block:: json

        {
          "agent_runtime":  {"configured": <bool>, "missing_env": [...], "docs": "...",
                             "maturity": "experimental", "target_ga": null,
                             "tracking": "https://github.com/evoila/meho/issues/2656"},
          "ui_surface":     {"configured": <bool>, "missing_env": [...], "docs": "...",
                             "maturity": "beta", "target_ga": "v1.0.0",
                             "tracking": "https://github.com/evoila/meho/issues/2665"},
          "audit_replay":   {"configured": true,   "capture_mode": "...", "missing_env": [],
                             "maturity": "ga"},
          "approval_queue": {"configured": <bool>, "depends_on": "agent_runtime",
                             "effective_posture": "four_eyes_enforced" |
                                                  "single_operator_break_glass",
                             "maturity": "ga"},
          "mcp":            {"configured": true,   "protocol_version": "...", "missing_env": []}
        }
    """
    block: dict[str, dict[str, Any]] = {
        "agent_runtime": _agent_runtime_block(settings),
        "ui_surface": _ui_surface_block(settings),
        "audit_replay": _audit_replay_block(),
        "approval_queue": _approval_queue_block(settings),
        "mcp": _mcp_block(),
    }
    for entry_name, feature_key in _READY_ENTRY_FEATURE.items():
        block[entry_name].update(FEATURE_MATURITY[feature_key])
    return block
