# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit-query forensic console UI routes.

Initiative #1841 (G10.15 Audit-query forensic console), Task #1944 (T1).

The package exposes the entry **query page** for the console's forensic
audit surface: a filter form over unbounded ``audit_log`` history,
forward-cursor "Load more" paging, and one-click pivots to the pre-canned
shortcuts (who-touched / by-work-ref) and to the T3 replay tree. T2 (#2)
adds the row detail drawer; T3 adds the replay tree -- T1 ships the chassis
those hang off.

Module layout:

* :mod:`~meho_backplane.ui.routes.audit.routes` -- the
  ``GET /ui/audit`` (full page) and ``GET /ui/audit/results`` (filter-submit
  + forward-cursor pager fragment) routes. All reads dispatch the
  :func:`meho_backplane.audit_query.query_audit` substrate in-process with
  ``tenant_id`` from the validated session, the same console-BFF pattern the
  approvals surface uses. The op_class colour palette is imported from the
  broadcast feed so audit rows colour-code identically.

The umbrella :func:`build_audit_router` is mounted **before**
:func:`meho_backplane.ui.routes.stubs.build_stubs_router` in
:func:`meho_backplane.ui.routes.build_router` so the real ``/ui/audit``
handler wins the first-match-wins lookup against the placeholder
``/ui/{slug}``; inside the router the literal ``/ui/audit/results`` route is
registered before the ``/ui/audit`` page route (and ahead of any future
``{param}`` route) so the literal segment is never bound as a slug.
"""

from __future__ import annotations

from meho_backplane.ui.routes.audit.routes import build_audit_router

__all__ = ["build_audit_router"]
