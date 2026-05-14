# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Public API for the targets package (G0.3 Targets-as-data).

Re-exports the four Pydantic schemas and the resolver surface so
callers can import from ``meho_backplane.targets`` without knowing
the internal module split.
"""

from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)
from meho_backplane.targets.schemas import (
    Target,
    TargetCreate,
    TargetSummary,
    TargetUpdate,
)

__all__ = [
    "AmbiguousTargetError",
    "Target",
    "TargetCreate",
    "TargetNotFoundError",
    "TargetSummary",
    "TargetUpdate",
    "resolve_target",
]
