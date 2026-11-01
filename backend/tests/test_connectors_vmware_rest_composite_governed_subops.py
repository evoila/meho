# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each vmware-rest composite carries its own child calls on its row.

The child calls (``governed_subops``) used to be one shared list in
``composites/_governed_subops.py``. Now each row in the composite's own
module carries them, and ``_register.py`` reads them from the rows. The
fixture ``fixtures/composites/vmware_rest_governed_subops.json`` is the
shared list as it was before the move. These tests check that every
composite in it may still call exactly the same child calls, in the same
order. A new composite is not in the fixture and needs no change there.
"""

from __future__ import annotations

import json
from pathlib import Path

from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    _GOVERNED_SUBOP_MANIFEST,
)

_BEFORE = Path(__file__).parent / "fixtures" / "composites" / "vmware_rest_governed_subops.json"


def _before() -> dict[str, list[str]]:
    data: dict[str, list[str]] = json.loads(_BEFORE.read_text(encoding="utf-8"))
    return data


def test_every_composite_keeps_its_child_calls() -> None:
    rows = {spec.op_id: spec for spec in _COMPOSITES}
    before = _before()
    assert len(before) == 73
    for op_id, sub_ops in before.items():
        assert op_id in rows, f"{op_id} is no longer registered"
        assert list(rows[op_id].governed_subops) == sub_ops, op_id


def test_published_list_is_read_from_the_rows() -> None:
    expected = {spec.op_id: spec.governed_subops for spec in _COMPOSITES if spec.governed_subops}
    assert expected == _GOVERNED_SUBOP_MANIFEST
    before = _before()
    for op_id in before:
        assert _GOVERNED_SUBOP_MANIFEST[op_id] == tuple(before[op_id])


def test_only_approval_composites_declare_child_calls() -> None:
    for spec in _COMPOSITES:
        if spec.governed_subops:
            assert spec.requires_approval, spec.op_id
            assert all(isinstance(sub_op, str) and sub_op for sub_op in spec.governed_subops)
