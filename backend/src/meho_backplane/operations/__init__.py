# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_backplane.operations`` -- dispatcher-facing operation registry.

The G0.6 substrate (#388) lives here. T1 (#392) ships the underlying
``endpoint_descriptor`` + ``operation_group`` tables; T3 (#394) extends
the :class:`~meho_backplane.connectors.base.Connector` ABC with the
registry-v2 metadata attrs; T4 (this package, #395) ships
:func:`register_typed_operation` -- the async helper typed connectors
call at init time to populate the tables.

The package deliberately exposes only the registration entry point in
v0.2. The dispatcher (T5, #396) reads ``endpoint_descriptor`` rows
directly via the ORM; the meta-tools (T8, #399) hit the same surface
via the retrieval helpers in
:mod:`meho_backplane.operations.search` (G0.6-T6 / T7 territory).
"""

from meho_backplane.operations.typed_register import (
    HandlerRefError,
    TypedOpHandler,
    register_typed_operation,
)

__all__ = [
    "HandlerRefError",
    "TypedOpHandler",
    "register_typed_operation",
]
