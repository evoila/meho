# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synthetic ids for the placeholders in parameter-schema examples.

An example uses neutral placeholders such as ``<vapp-id>``, never a value
that looks real, so a model does not copy it as a real choice. A field with
an id pattern (``^resgroup-v[0-9]+$``, ...) rejects such a placeholder, as it
must for real input. The example checks swap each known placeholder for a
made-up id of the right shape before they validate; the schema itself stays
strict. A placeholder not in the table stays as it is, so a name like
``<vm-name>`` validates as a plain string and an id placeholder without a row
fails the check (add the row).
"""

from __future__ import annotations

from typing import Any, Final

#: placeholder -> a made-up id of the shape the field's pattern wants.
SYNTHETIC_IDS: Final[dict[str, str]] = {
    "<cluster-id>": "domain-c1",
    "<resource-pool-id>": "resgroup-1",
    "<vm-folder-id>": "group-v1",
    "<vapp-id>": "resgroup-v1",
    "<vm-id>": "vm-1",
    "<other-vm-id>": "vm-2",
}


def with_synthetic_ids(example: Any) -> Any:
    """*example* with every known placeholder swapped for its synthetic id."""
    if isinstance(example, str):
        return SYNTHETIC_IDS.get(example, example)
    if isinstance(example, dict):
        return {key: with_synthetic_ids(value) for key, value in example.items()}
    if isinstance(example, list):
        return [with_synthetic_ids(item) for item in example]
    return example
