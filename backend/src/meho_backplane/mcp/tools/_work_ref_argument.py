# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read a ticket id (work ref) search filter of an MCP tool with the shared rule.

The rule is :func:`meho_backplane.work_ref.normalize_work_ref`. A value that
breaks it is refused as invalid params, with a message that names the rule
and never the refused text beyond the code point. A good value keeps its
text as typed: the search matches it with
:func:`meho_backplane.work_ref.work_ref_filter_values`, so rows stored before
the rule are still found by their exact old text.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.work_ref import WorkRefError, check_work_ref_filter_input

__all__ = ["work_ref_argument"]


def work_ref_argument(value: Any, *, name: str = "work_ref") -> str | None:
    """Return the checked ticket id filter of a tool argument, ``None`` when absent.

    ``name`` is the argument name used in the error message, for example
    ``filter.work_ref``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpInvalidParamsError(f"{name} must be a string when supplied")
    try:
        checked: str = check_work_ref_filter_input(value)
    except WorkRefError as exc:
        raise McpInvalidParamsError(str(exc).replace("work_ref", name, 1)) from exc
    return checked
