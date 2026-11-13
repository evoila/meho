# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-class badge colours and filter options of the audit console.

These tables lived with the Broadcast feed while it showed operations.
Broadcast shows only agent announcements since #3762, so the audit
console, which still lists operations, owns them now.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.broadcast import OP_CLASS_ENUM

__all__ = ["OP_CLASS_BADGE_CLASSES", "OP_CLASS_FILTER_OPTIONS"]

#: Map from the closed ``op_class`` vocabulary
#: (:func:`meho_backplane.broadcast.classify_op`) to DaisyUI badge
#: variant classes, covering every :data:`OP_CLASS_ENUM` member. The
#: secret-material classes -- ``credential_read`` / ``credential_write``
#: / ``credential_mint`` -- get the warning palette so an operator reads
#: the sensitivity at a glance; ``audit_query`` and ``approval`` are
#: informational (info); ``checks`` is primary; ``write`` is accent
#: (mutation); ``read`` and ``other`` are the neutral ghost. A class not
#: in this map falls back to ``badge-ghost``.
OP_CLASS_BADGE_CLASSES: Final[dict[str, str]] = {
    "read": "badge-ghost",
    "write": "badge-accent",
    "credential_read": "badge-warning",
    "credential_write": "badge-warning",
    "credential_mint": "badge-warning",
    "audit_query": "badge-info",
    "approval": "badge-info",
    "checks": "badge-primary",
    "other": "badge-ghost",
}

#: The op_class filter dropdown options: the full closed vocabulary,
#: single-sourced from :data:`OP_CLASS_ENUM` (#2731). The empty "All"
#: sentinel is template-side.
OP_CLASS_FILTER_OPTIONS: Final[tuple[str, ...]] = OP_CLASS_ENUM
