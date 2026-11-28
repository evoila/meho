# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Save the op class marks before a test and put them back after it.

Connectors declare their op class marks once, at import time, in the
module-level containers of :mod:`meho_backplane.op_classes`. A test that
calls ``_reset_declared_op_classes_for_testing()`` empties all of them.
The import does not run again, so a test that empties the marks must put
every one of them back, or later tests in the same process see no marks.

``kept_op_class_state`` does this for every name in ``OP_CLASS_MARKS``.
``test_op_class_state.py`` checks that this list names every mark of the
module.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from meho_backplane import op_classes

#: Every module-level mark of :mod:`meho_backplane.op_classes`.
OP_CLASS_MARKS: tuple[str, ...] = (
    "_DECLARED",
    "_SAFE_WRITES",
    "_ROOT_SHELL",
    "_PER_CALL_APPROVAL",
    "_NO_SECRET_VALUE",
    "_WAIT_WHEN_SET",
    "_CREDENTIAL_CAPABLE",
    "_CREDENTIAL_CAPABLE_MATCHES",
    "_SANDBOX_DELETE_GRANT",
    "_CREDENTIAL_CHECKS",
    "_HIDDEN_STORED_PARAMS",
    "_HIDDEN_STORED_MAP_VALUES",
)


@contextmanager
def kept_op_class_state(*, start_empty: bool = False) -> Iterator[None]:
    """Put every op class mark back as it was, when the block ends.

    With ``start_empty=True`` the block starts with all marks empty.
    """
    saved: dict[str, Any] = {name: copy.copy(getattr(op_classes, name)) for name in OP_CLASS_MARKS}
    if start_empty:
        op_classes._reset_declared_op_classes_for_testing()
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        for name, values in saved.items():
            mark = getattr(op_classes, name)
            if isinstance(mark, list):
                mark.extend(values)
            else:
                mark.update(values)
