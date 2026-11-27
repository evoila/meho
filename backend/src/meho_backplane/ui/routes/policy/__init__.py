# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Policy" page of the web console (``/ui/policy``)."""

from __future__ import annotations

from meho_backplane.ui.routes.policy.page import POLICY_NEEDS_ADMIN, build_policy_router

__all__ = ["POLICY_NEEDS_ADMIN", "build_policy_router"]
