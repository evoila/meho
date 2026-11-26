# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The shared test helper puts every op class mark back.

``tests/_op_class_state.py`` saves the module-level marks of
:mod:`meho_backplane.op_classes` and puts them back after a test that
empties them. These tests fail when the module gets a new mark that the
helper does not name, or when the helper does not put a mark back.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from meho_backplane import op_classes

from ._op_class_state import OP_CLASS_MARKS, kept_op_class_state

_MARK_NAME = re.compile(r"_[A-Z][A-Z0-9_]*")


def _module_marks() -> set[str]:
    return {
        name
        for name, value in vars(op_classes).items()
        if _MARK_NAME.fullmatch(name) and isinstance(value, dict | set | list)
    }


def _stand_in() -> bool:
    return True


def test_the_helper_names_every_mark_of_the_module() -> None:
    assert set(OP_CLASS_MARKS) == _module_marks()


def test_every_mark_is_back_after_a_reset_inside_the_helper() -> None:
    stand_in_id = "example.stand_in.op"
    names = sorted(_module_marks())
    own_copy = {name: copy.copy(getattr(op_classes, name)) for name in names}
    added: dict[str, Any] = {}
    for name in names:
        mark = getattr(op_classes, name)
        if isinstance(mark, list):
            mark.append(_stand_in)
            added[name] = _stand_in
        elif isinstance(mark, set):
            mark.add(stand_in_id)
            added[name] = stand_in_id
        else:
            mark[stand_in_id] = _stand_in
            added[name] = stand_in_id
    try:
        with kept_op_class_state():
            op_classes._reset_declared_op_classes_for_testing()
        missing = sorted(
            name for name, value in added.items() if value not in getattr(op_classes, name)
        )
        assert missing == []
    finally:
        # Put the real marks back here too, so a failing helper does not
        # leave the marks empty for the next tests in this process.
        for name, values in own_copy.items():
            mark = getattr(op_classes, name)
            mark.clear()
            if isinstance(mark, list):
                mark.extend(values)
            else:
                mark.update(values)
