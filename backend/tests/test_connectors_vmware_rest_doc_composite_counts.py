# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check that the read / write split of the vmware-rest composites adds up.

A read is a composite with ``requires_approval=False``. That includes
``vm.guest.file.read``, which is ``caution`` but does not need approval.
Every write needs approval and is never ``safe``.

This test reads only the ``_COMPOSITES`` registry. The connector doc does
not state how many composites exist, so nothing here reads the doc.
"""

from __future__ import annotations

from collections import Counter

from meho_backplane.connectors.vmware_rest.composites._register import _COMPOSITES

_WRITES = sum(1 for spec in _COMPOSITES if spec.requires_approval)
_WRITE_LEVELS = Counter(spec.safety_level for spec in _COMPOSITES if spec.requires_approval)


def test_registry_split_is_consistent() -> None:
    """Every write needs approval and is not ``safe``; the levels add up."""
    assert sum(_WRITE_LEVELS.values()) == _WRITES
    assert "safe" not in _WRITE_LEVELS
