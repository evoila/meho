# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approvals UI surface: notifications bell + approve/deny modal over a session BFF.

Initiative #1775 (G10.7 Operator-console hardening), Task #1778. The
operator-console face of the approval queue (Goal #800): a bell/badge in
the app-shell fed live by the SSE bridge, opening a modal that approves or
denies a pending :class:`~meho_backplane.db.models.ApprovalRequest`.

* ``GET /ui/approvals/badge`` -- the live pending-count bell badge.
* ``GET /ui/approvals`` -- the pending-requests panel (modal body).
* ``GET /ui/approvals/{id}`` -- the request-detail + approve/deny modal.
* ``POST /ui/approvals/{id}/approve`` / ``.../reject`` -- the decisions,
  calling the :mod:`~meho_backplane.operations.approval_queue` service
  in-process (session + CSRF gated -- NOT the Bearer ``/api/v1/approvals``
  routes, which a session cookie cannot authenticate).

The router is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router`, and the literal ``badge``
segment is registered ahead of the ``/{request_id}`` slug route so it is
never bound as the id parameter.
"""

from __future__ import annotations

from meho_backplane.ui.routes.approvals.routes import build_approvals_router

__all__ = ["build_approvals_router"]
