# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""salt spec-reconcile lane -- hand-coded RaaS RPC methods vs the pinned reference list.

Broadcom publishes no API file for the Salt RaaS API. The spec shelf pins a
reference list made from the vendor's public RaaS 8.18 reference pages
(``salt-raas-8.18/salt-raas-8.18-reference-operations.tsv``): one line per
RPC method, ``resource.method`` in the first column.

Every RPC method the salt connector calls is a module constant in
:mod:`meho_backplane.connectors.salt.connector` whose name ends in
``_RPC``. The lane reads them from the live module (never a copy) and
checks each against the list. The two HTTP bridge paths (``GET
/account/login`` and ``POST /rpc``) come from the reference page "API
Usage", not from the method list, so they are pinned in the manifest only.

VCF 9.1 and 9.1.1 Salt have no public reference yet; their lists are added
to :data:`_REFERENCE_LISTS` once the lab pins them (``api.discover`` on a
running server).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meho_backplane.connectors.salt import connector as _connector_module
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

#: One ``(shelf directory, file)`` per version with a pinned reference list.
_REFERENCE_LISTS = (("salt-raas-8.18", "salt-raas-8.18-reference-operations.tsv"),)

_HEADER = ["resource.method", "parameters", "section", "reference page URL"]


def _declared_rpc_methods() -> set[str]:
    """The RPC methods the connector calls, as ``resource.method``."""
    return {
        f"{value[0]}.{value[1]}"
        for name, value in vars(_connector_module).items()
        if name.endswith("_RPC") and isinstance(value, tuple)
    }


def _reference_methods(list_path: Path, label: str) -> set[str]:
    lines = list_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].split("\t") != _HEADER:
        pytest.fail(f"{label}: expected the TSV header {_HEADER!r}.")
    return {line.split("\t")[0] for line in lines[1:] if line.strip()}


def test_salt_hand_coded_rpc_manifest_is_pinned() -> None:
    """Pin the swept method set so drift cannot silently shrink the reconcile."""
    assert _declared_rpc_methods() == {
        "api.get_versions",
        "master.get_master_presence",
        "minions.get_minion_cache",
        "minions.get_minion_presence",
        "minions.get_minion_key_state",
        "cmd.get_cmds",
        "ret.get_returns",
        "tgt.get_target_group",
    }
    assert (_connector_module.LOGIN_PATH, _connector_module.RPC_PATH) == (
        "/account/login",
        "/rpc",
    )


@pytest.mark.parametrize(("spec_dir", "spec_file"), _REFERENCE_LISTS)
def test_salt_rpc_methods_are_in_the_pinned_reference(spec_dir: str, spec_file: str) -> None:
    """Every hand-coded RPC method is in the pinned RaaS reference list."""
    label = f"{spec_dir}/{spec_file}"
    served = _reference_methods(require_shelf_spec(spec_dir, spec_file), label)
    assert_op_ids_served(_declared_rpc_methods(), served, spec_label=label)
