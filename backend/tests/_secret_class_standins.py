# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Stand-in operations for the classes whose request carries a secret.

A test that checks how MEHO hides the params of a ``credential_write`` call
runs for ``credential_use`` too. Each class gets one stand-in op id that no
name rule knows, so only its declared class decides how MEHO treats it. The
made-up value is built at run time, holds no secret word, and sits under the
param name :data:`NOTE`, which no name rule hides.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.op_classes import _DECLARED, declare_op_classes

#: The classes whose request carries a secret, each with its stand-in op id.
STANDIN_OPS: dict[str, str] = {
    "credential_write": "standin.notes.send",
    "credential_use": "standin.notes.fetch",
}

#: A param name that no name rule hides.
NOTE = "note"


def made_up_value() -> str:
    """A value that only this run knows, with no secret word in it."""
    return f"plain-{uuid.uuid4().hex}"


@pytest.fixture
def standin_ops() -> Iterator[dict[str, str]]:
    """Declare each stand-in op id with its class, as a connector would."""
    for op_class, op_id in STANDIN_OPS.items():
        declare_op_classes(**{op_class: (op_id,)})
    try:
        yield STANDIN_OPS
    finally:
        for op_id in STANDIN_OPS.values():
            _DECLARED.pop(op_id, None)
