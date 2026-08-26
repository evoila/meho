# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Reflex-adoption KPIs computed over the append-only ``audit_log``.

The reflex work (tool descriptions, plugin hooks, dispatch advisory)
aims at behavioural change; this package measures whether that change
is happening, directly from the audited record MEHO already keeps. See
:mod:`meho_backplane.reflex.adoption` for the metric definitions.
"""

from meho_backplane.reflex.adoption import (
    ReflexReport,
    SurfaceMetrics,
    compute_reflex_report,
)

__all__ = [
    "ReflexReport",
    "SurfaceMetrics",
    "compute_reflex_report",
]
