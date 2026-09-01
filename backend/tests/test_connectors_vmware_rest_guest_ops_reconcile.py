# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-spec reconcile lane for the guest-operations channel sub-op paths (#3100).

Mirrors ``test_connectors_vmware_rest_composites_read_reconcile.py`` for
the ``composites/_guest.py`` module: every ``_OP_*`` constant the guest
handlers dispatch through ``_post_vmomi_json`` is a vmomi (VI-JSON)
method, so each must be served by the pinned ``vcenter-9.0/vi-json.yaml``
per the spec-reconcile standard
(``docs/decisions/spec-reconcile-guards-standard.md``). The constants are
introspected live (never a hardcoded mirror that can drift), and the
shelf-backed lane skips uniformly when the spec shelf is unprovisioned
(the #2980 harness contract) -- so it runs for real only in the
shelf-armed CI lane, not the public per-PR gate. The always-on pin test
freezes the constant strings so a typo in a guest-method path is caught
even without the shelf.
"""

from __future__ import annotations

from meho_backplane.connectors.vmware_rest.composites import _guest
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)


def _swept_op_ids() -> set[str]:
    """Every ``_OP_*`` op_id constant in ``_guest``, introspected live."""
    ops: set[str] = set()
    for name in dir(_guest):
        if not name.startswith("_OP_"):
            continue
        value = getattr(_guest, name)
        if isinstance(value, str):
            ops.add(value)
    return ops


def test_guest_op_constants_are_all_vmomi_post_legs() -> None:
    """Pin the guest-ops op_id strings (sandbox-safe, always runs).

    Every guest sub-op is a vmomi POST routed through
    ``_post_vmomi_json`` -- there is no Automation GET leg -- so the
    shelf-backed lane below reconciles the whole set against
    ``vi-json.yaml``. This pin freezes the strings so a path typo is
    caught even where the spec shelf is absent.
    """
    swept = _swept_op_ids()
    assert swept == {
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/GuestProcessManager/{moId}/ListProcessesInGuest",
        "POST:/GuestProcessManager/{moId}/ReadEnvironmentVariableInGuest",
        "POST:/GuestProcessManager/{moId}/StartProgramInGuest",
        "POST:/GuestFileManager/{moId}/InitiateFileTransferFromGuest",
        "POST:/GuestFileManager/{moId}/InitiateFileTransferToGuest",
    }
    assert all(op_id.startswith("POST:") for op_id in swept)


def test_guest_vim_sub_ops_are_served_by_the_pinned_vi_json_spec() -> None:
    """Every guest-ops vmomi method is served by the pinned vi-json.yaml."""
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_swept_op_ids(), served, spec_label="vcenter-9.0/vi-json.yaml")
