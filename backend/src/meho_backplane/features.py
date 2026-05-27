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
  no separate setup doc to read) and on the transitive
  ``approval_queue`` entry (which surfaces a ``depends_on`` field
  pointing at ``agent_runtime`` instead).

The audit-replay entry additionally exposes ``capture_mode`` —
``"enforced"`` until G0.14-T6 (#1147) decouples capture from
enforcement, ``"always"`` after. The shape is forward-compatible: T6
flips this from ``"enforced"`` to ``"always"`` and adds nothing else.

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
"""

from __future__ import annotations

from typing import Any

from meho_backplane.settings import Settings

__all__ = ["build_features_block"]


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

    Configured iff both :attr:`Settings.ui_keycloak_client_id` and
    :attr:`Settings.ui_keycloak_client_secret` are non-empty. Either
    unset surfaces as a 503 from ``GET /ui/auth/login`` carrying the
    gold-standard :data:`~meho_backplane.ui.auth.flow.MISSING_CLIENT_SECRET_DETAIL`
    message; the doc reference here matches the one that 503 cites.
    """
    missing: list[str] = []
    if not settings.ui_keycloak_client_id:
        missing.append("UI_KEYCLOAK_CLIENT_ID")
    if not settings.ui_keycloak_client_secret:
        missing.append("UI_KEYCLOAK_CLIENT_SECRET")
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


def _approval_queue_block(settings: Settings) -> dict[str, Any]:
    """Whether the agent-grant approval queue is operative.

    The approval queue is transitive on the agent runtime: if the
    agent-principal surface is unreachable (Keycloak admin not
    configured), no agent has identity, so the approval queue has
    nothing to queue against. The block exposes
    ``depends_on="agent_runtime"`` instead of a separate
    ``missing_env`` so the operator's remediation chain is one step
    deep ("configure agent_runtime, the queue activates").
    """
    agent_runtime = _agent_runtime_block(settings)
    return {
        "configured": agent_runtime["configured"],
        "depends_on": "agent_runtime",
    }


def build_features_block(settings: Settings) -> dict[str, dict[str, Any]]:
    """Return the ``features`` block exposed under ``/ready``.

    Pure function over a :class:`Settings` snapshot — no env reads, no
    cache mutation, no I/O. Callers compose this into the ``/ready``
    payload directly; tests build a :class:`Settings` instance with
    the attribute values under test and assert against the returned
    dict.

    The four entries (``agent_runtime``, ``ui_surface``,
    ``audit_replay``, ``approval_queue``) match the four gated
    features the v0.6.0 release ships. The block is **closed** by
    design: adding a new gated feature is an additive change to this
    function and the corresponding entry in the
    ``docs/RELEASING.md`` post-deploy enablement section; renaming
    one is a wire-compat break for operator tooling that reads
    ``/ready``.

    Shape (verified by the issue body):

    .. code-block:: json

        {
          "agent_runtime":  {"configured": <bool>, "missing_env": [...], "docs": "..."},
          "ui_surface":     {"configured": <bool>, "missing_env": [...], "docs": "..."},
          "audit_replay":   {"configured": true,   "capture_mode": "...", "missing_env": []},
          "approval_queue": {"configured": <bool>, "depends_on": "agent_runtime"}
        }
    """
    return {
        "agent_runtime": _agent_runtime_block(settings),
        "ui_surface": _ui_surface_block(settings),
        "audit_replay": _audit_replay_block(),
        "approval_queue": _approval_queue_block(settings),
    }
