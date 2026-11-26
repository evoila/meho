# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every operation with a role floor is named in the texts people read.

The install page part "If you are the only admin" and the ``meho status``
texts (``cli/internal/output/format.go``) name the groups of operations
that need the Admin role: ``targets.``, ``topology.``, ``spaces.``,
``policy.`` and ``tools.sign_out``. A new operation with a floor outside
these groups makes this test fail.
"""

from __future__ import annotations

import importlib

from meho_backplane.operations import role_floors

# The modules that register a role floor when they are imported.
_FLOOR_MODULES = (
    "meho_backplane.connectors.targets.ops",
    "meho_backplane.connectors.targets.update",
    "meho_backplane.connectors.policy",
    "meho_backplane.connectors.topology.ops",
    "meho_backplane.connectors.tools",
    "meho_backplane.connectors.spaces.registry",
)

_NAMED_PREFIXES = ("targets.", "topology.", "spaces.", "policy.")
_NAMED_OPERATIONS = ("tools.sign_out",)


def test_every_role_floor_operation_is_in_a_named_group() -> None:
    for module in _FLOOR_MODULES:
        importlib.import_module(module)

    floored = sorted(op_id for op_id in role_floors._FLOORS if role_floors.role_floor_for(op_id))
    assert floored, "no operation has a role floor; the floor modules were not imported"

    unnamed = [
        op_id
        for op_id in floored
        if not op_id.startswith(_NAMED_PREFIXES) and op_id not in _NAMED_OPERATIONS
    ]
    assert not unnamed, (
        f"These operations have a role floor but are in no named group: {unnamed}. "
        "Add the group to the Admin-role item in the part 'If you are the only admin' "
        "of docs-site/install/form-3-your-kubernetes.md, and to the texts of "
        "meho status in cli/internal/output/format.go."
    )
