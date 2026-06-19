# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Operations launcher UI package.

Initiative #1835 (G10.9 Operations console), Task #1879 (T1). The
read-only launcher surface: a connector picker, an operation-group
browse + hybrid free-text search, and an operation detail drawer.

The router (:func:`build_operations_router`) is a thin BFF over the
operation meta-tools (:mod:`meho_backplane.operations.meta_tools`)
called in-process, mirroring the approvals surface's session-BFF
precedent rather than the Bearer-gated REST routes in
:mod:`meho_backplane.api.v1.operations`.
"""

from __future__ import annotations

from meho_backplane.ui.routes.operations.routes import build_operations_router

__all__ = ["build_operations_router"]
