# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""BFF audit-thread -- bind audit contextvars for ``/ui/*`` page views.

Initiative #1209 (G0.15 v0.7.0 closed-loop dogfood hardening), Task
#1216 (T7). The consumer's v0.7.0 closed-loop dogfood
(``claude-rdc-hetzner-dc#753``) flagged the governance product
completeness gap: an operator browsing 5 ``/ui/*`` surfaces generated
**zero** ``audit_log`` rows under their ``principal_sub``. The agent
path (``/mcp`` + ``/api/v1/*``) is audited correctly per-request;
the operator's browser path is not.

Root cause
----------

The chassis :class:`~meho_backplane.audit.AuditMiddleware` skips
unauthenticated requests by checking the ``operator_sub`` structlog
contextvar; requests without that binding produce no audit row
(public surfaces, 401s, paths the JWT dependency hasn't reached).
The :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
resolves the operator from the session cookie and stashes the
identity on ``request.state`` and the :class:`UISessionContext`,
but historically did **not** bind ``operator_sub`` / ``tenant_id``
into the structlog contextvars the audit middleware reads. Only
:func:`~meho_backplane.ui.auth.middleware.require_ui_admin` bound
them, and only for write surfaces that depend on it. Read GETs
through :func:`~meho_backplane.ui.auth.middleware.require_ui_session`
silently bypassed the audit middleware's skip rule.

Fix
---

This module exposes :func:`bind_ui_view_audit` which the UI session
middleware calls after successful session resolution for every
``/ui/*`` page-view request. It binds three structlog contextvars
the audit middleware consumes:

* ``operator_sub`` -- session's ``principal_sub`` (lifts AuditMiddleware's
  unauthenticated-skip and writes the operator-attributed row).
* ``tenant_id`` -- session's tenant UUID as a string (matches the JWT
  path's binding shape from
  :func:`~meho_backplane.middleware.verify_jwt_and_bind`).
* ``audit_op_id`` / ``audit_op_class`` -- the row's operation
  identifiers. The path's surface (``connectors``, ``kb``, ``memory``,
  ``broadcast``, ``topology``, ``dashboard``) is mapped to a stable
  ``ui.view.<surface>`` op_id; ``op_class`` is ``"ui_view"`` so
  operators can opt-in/out of UI views in their forensic timeline
  via the existing ``query_audit op_class=ui_view`` filter (the
  issue body's Option B -- a new class rather than reusing
  ``read``, so retention/query policies can prune UI views
  independently when a governance regime prefers it).

The op_id is path-derived rather than route-bound (no per-route
``bind_contextvars`` call). Centralising the mapping in one helper
means a new ``/ui/<surface>`` Initiative cannot accidentally ship
a surface without audit coverage -- the surface is audited the
moment its route lives under a recognised prefix.

Out of scope here
-----------------

* Write requests (``POST`` / ``PATCH`` / ``DELETE`` from HTMX forms)
  -- those flow through service-layer functions
  (``create_target`` / ``update_target`` / ``forget_memory`` etc.)
  that already write audit rows under their own ``op_id`` /
  ``op_class`` discipline. Surfacing a duplicate ``ui_view`` row
  per write would double the audit footprint of every state change.
  The session middleware binds the ``ui_view`` op_id/op_class only
  for GET requests (and HEAD, which the chassis treats identically).
* Target-scoped target_id binding -- already happens at the
  resolver layer:
  :func:`~meho_backplane.targets.resolver.resolve_target` binds
  ``target_id`` into structlog contextvars on success, and the
  audit middleware reads it into the typed
  :attr:`~meho_backplane.db.models.AuditLog.target_id` column.
  No additional plumbing required here for ``/ui/connectors/<name>``.

References
----------

* Issue #1216 (G0.15-T7) -- the BFF audit-thread gap.
* :class:`~meho_backplane.audit.AuditMiddleware` -- the chassis
  audit writer; consumes the contextvars this module binds.
* :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware` --
  the caller; binds session identity onto the request scope.
* G0.15-T3 (#1212) -- mirrors the MCP outer-wrapper column hoisting
  pattern; this Task is the BFF analog for the HTTP/UI path.
"""

from __future__ import annotations

from typing import Final

import structlog

__all__ = [
    "UI_AUDIT_OP_CLASS",
    "bind_ui_view_audit",
    "derive_ui_surface",
]


#: ``op_class`` value written for every ``/ui/*`` page view.
#:
#: Distinguishes UI page-view reads from the agent path's ``read`` class
#: so operators can filter them in/out independently --
#: ``meho audit query op_class=ui_view`` returns only UI page views, the
#: complement returns the agent + REST traffic. The consumer's v0.7.0
#: closed-loop feedback (``claude-rdc-hetzner-dc#753``) proposed Option B
#: (new class) over Option A (reuse ``read``) so retention policies can
#: prune the high-cardinality UI view trail independently of the
#: governance-load-bearing agent dispatch trail.
UI_AUDIT_OP_CLASS: Final[str] = "ui_view"

#: Single source of truth for ``/ui/<surface>`` → audit surface name.
#:
#: The prefix is matched longest-first by :func:`derive_ui_surface` so
#: a future ``/ui/connectors-foo`` sub-Initiative cannot accidentally
#: shadow the more general ``/ui/connectors`` surface. Order is the
#: declaration order in this tuple; keep longest-prefix entries before
#: their parents.
_UI_SURFACE_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("/ui/broadcast", "broadcast"),
    ("/ui/connectors", "connectors"),
    ("/ui/kb", "kb"),
    ("/ui/memory", "memory"),
    ("/ui/topology", "topology"),
)

#: Surface label for the dashboard root. ``/ui/`` (with the trailing
#: slash) is the FastAPI route the chassis registers; ``/ui`` (no
#: trailing slash) goes through Starlette's redirect to ``/ui/``
#: before this helper sees it, so the exact-equality match is safe.
_DASHBOARD_PATH: Final[str] = "/ui/"
_DASHBOARD_SURFACE: Final[str] = "dashboard"


def derive_ui_surface(path: str) -> str | None:
    """Map a ``/ui/*`` path to its surface label.

    Returns the surface name (``"connectors"``, ``"kb"``, ``"memory"``,
    ``"broadcast"``, ``"topology"``, ``"dashboard"``) when *path* lives
    under a recognised UI prefix; returns ``None`` otherwise.

    The matching rule is "starts-with the prefix followed by either
    end-of-string or a path separator". This catches both the bare
    surface page (``/ui/kb``) and any nested page or HTMX partial
    (``/ui/kb/some-slug``, ``/ui/kb/some-slug/preview``,
    ``/ui/memory/operator/foo/edit``). The exact ``/ui/`` literal is
    the dashboard; ``/ui`` without the slash is a Starlette redirect
    that bypasses this audit binding entirely (the 307 response has no
    operator to attribute regardless).
    """
    if path == _DASHBOARD_PATH:
        return _DASHBOARD_SURFACE
    for prefix, label in _UI_SURFACE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return label
    return None


def bind_ui_view_audit(
    *,
    operator_sub: str,
    tenant_id: str,
    path: str,
) -> None:
    """Bind audit contextvars for a ``/ui/<surface>`` page-view request.

    Called by :class:`~meho_backplane.ui.auth.middleware.UISessionMiddleware`
    after successful session resolution, before the inner ASGI app
    runs the route handler. Binds:

    * ``operator_sub`` + ``tenant_id`` -- lifts
      :class:`~meho_backplane.audit.AuditMiddleware`'s
      unauthenticated-skip rule and provides the typed columns the
      audit row needs.
    * ``audit_op_id`` -- ``ui.view.<surface>`` derived from *path*; the
      audit middleware strips the ``audit_`` prefix and writes the
      remainder into the row's ``payload['op_id']``.
    * ``audit_op_class`` -- :data:`UI_AUDIT_OP_CLASS` (``"ui_view"``).

    Routes that resolve a specific target by name (``/ui/connectors/<name>``)
    have ``target_id`` bound separately by
    :func:`~meho_backplane.targets.resolver.resolve_target` at its
    single exit point (G0.3-T4 contract); the audit middleware reads
    that contextvar into the row's
    :attr:`~meho_backplane.db.models.AuditLog.target_id` typed column.

    No-op when *path* does not map to a recognised UI surface
    (auth / static / out-of-prefix). The caller is responsible for
    only invoking this on authenticated ``/ui/*`` requests where a
    session was resolved.

    Idempotent on repeated calls within the same request -- the last
    binding wins, which matches structlog's ``bind_contextvars``
    semantics. The middleware never calls this twice per request.
    """
    surface = derive_ui_surface(path)
    if surface is None:
        # /ui/auth/*, /ui/static/*, and out-of-prefix paths are
        # filtered upstream by UISessionMiddleware; reaching this
        # branch means a future surface was registered under /ui/
        # but not added to _UI_SURFACE_PREFIXES. Bind operator
        # identity anyway so the chassis audit middleware still
        # writes the row (with the default http.get:/ui/xyz op_id),
        # otherwise the gap silently re-opens. The maintainer's
        # signal is the row landing under the default op_id rather
        # than ui.view.<surface>.
        structlog.contextvars.bind_contextvars(
            operator_sub=operator_sub,
            tenant_id=tenant_id,
        )
        return

    structlog.contextvars.bind_contextvars(
        operator_sub=operator_sub,
        tenant_id=tenant_id,
        audit_op_id=f"ui.view.{surface}",
        audit_op_class=UI_AUDIT_OP_CLASS,
    )
