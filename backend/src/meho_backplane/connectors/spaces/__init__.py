# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.spaces: operations on spaces and their assignments.

A built-in typed connector with no vendor behind it (like ``audit``). Its
wire id is ``spaces-records-1.x``. Importing the package queues
:func:`~meho_backplane.connectors.spaces.registry.register_spaces_operations`
onto the lifespan-driven registrar list, so the operation rows exist before
the first call. See ``docs/codebase/spaces.md``.
"""

from meho_backplane.connectors.spaces.registry import register_spaces_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

register_typed_op_registrar(register_spaces_operations)

__all__ = ["register_spaces_operations"]
