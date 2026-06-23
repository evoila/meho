# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operator-role lift for the scheduler UI surface.

Initiative #1824 (G10.8 Autonomous execution control plane), Task #1826
(T6). The scheduler surface gates the same way every other write-bearing
``/ui/*`` surface does: reads (the trigger list + detail) are
``operator``; writes (create + cancel) are ``tenant_admin``.

Rather than re-derive the JWT round-trip, this module re-exports the
two dependencies the connectors surface already ships
(:mod:`meho_backplane.ui.routes.connectors.operator`):

* :func:`resolve_role_probe` -- the read-path dependency. Projects the
  BFF session's operator role into the boolean the list / detail
  templates read (``is_tenant_admin``) to soft-hide the create / cancel
  affordances. Fails **soft** (no-privileges probe) on a transient
  JWT-validation hiccup so the read surface keeps rendering; the write
  routes remain the security authority.
* :func:`resolve_operator_or_403` -- the write-path dependency. Lifts
  the full :class:`~meho_backplane.auth.operator.Operator` and asserts
  ``tenant_admin``; a non-admin caller raises 403. Used by the
  create-trigger + cancel handlers so the create / cancel writes
  re-check the role server-side even when an operator forges the POST
  past the hidden button.

The single re-export keeps one JWT-lift implementation across the
console (the connectors module owns it) and one mental model for the
soft-hide-vs-hard-gate split.
"""

from __future__ import annotations

from meho_backplane.ui.routes.connectors.operator import (
    OperatorRoleProbe,
    resolve_operator_or_403,
    resolve_role_probe,
)

__all__ = [
    "OperatorRoleProbe",
    "resolve_operator_or_403",
    "resolve_role_probe",
]
