# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit console's op-class tables cover the closed vocabulary (#2731, #3762)."""

from __future__ import annotations

from meho_backplane.broadcast import OP_CLASS_ENUM
from meho_backplane.ui.routes.audit.op_class_badges import (
    OP_CLASS_BADGE_CLASSES,
    OP_CLASS_FILTER_OPTIONS,
)


def test_badge_and_filter_tables_cover_the_full_op_class_enum() -> None:
    """Set equality, so a class removed from the enum cannot linger either."""
    assert set(OP_CLASS_BADGE_CLASSES) == set(OP_CLASS_ENUM)
    assert set(OP_CLASS_FILTER_OPTIONS) == set(OP_CLASS_ENUM)
