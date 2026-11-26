# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reason field of a console form.

A console change that turns a control off asks for a written reason: turning
approval off for an operation, and the loosening changes of the settings file
page. Both read the field the same way.
"""

from __future__ import annotations

from typing import Final

__all__ = ["MAX_REASON_CHARS", "clean_reason"]

#: The longest reason a form accepts.
MAX_REASON_CHARS: Final[int] = 1024


def clean_reason(reason: str | None) -> str | None:
    """*reason* without the blanks around it; ``None`` when nothing is left."""
    return (reason or "").strip() or None
