# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""sddc-manager real-spec reconcile lane (#2982) — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the sddc-manager connector
dispatches is served by the pinned ``sddc-manager-9.0`` spec on the
shelf (``vmware/vcf-api-specs``' OpenAPI 3.0.1 document — Apache-2.0,
provenance in the shelf's ``MANIFEST.md``). Parse-only set compare —
no DB, no embeddings, no containers — so it lives in the required unit
sweep per the lane-placement rule in
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when
the shelf is unconfigured (``tests/_spec_shelf.py`` contract).

The declared set is introspected from the connector's live constants
(the #2944 pattern — never a hardcoded mirror), across the three
surfaces that hand-code a path:

* :mod:`~meho_backplane.connectors.sddc_manager.typed_reads` — the 14
  ``_*_PATH`` module constants. Every typed read dispatches through
  :meth:`HttpConnector._get_json` (a GET-only seam that delegates to
  ``_request_json(target, "GET", ...)``), so each declares ``GET:``.
* :mod:`~meho_backplane.connectors.sddc_manager.connector` — the token
  mint ``_SESSION_CREATE_PATH`` (``POST /v1/tokens``, sourced from the
  ``session_login_token`` scheme spec). The connector's bespoke
  fingerprint probe GETs the literal ``"/v1/sddc-managers"`` inline; the
  same string is declared here via ``_SDDC_MANAGERS_PATH``, so the probe
  path is covered by value.
* :mod:`~meho_backplane.connectors.sddc_manager.profile` — the
  declarative fingerprint recipe's ``GET /v1/releases/system``.

A red lane here is the guard surfacing a real finding, not harness
noise — first run against the real shelf caught exactly one:
``GET:/v1/domains/{id}/status`` (repointed to ``GET:/v1/domains/{id}``
in the same PR; the 9.0 spec serves no ``/status`` sub-resource).
Protocol: docs/decisions/spec-reconcile-guards-standard.md.
"""

from __future__ import annotations

from meho_backplane.connectors.sddc_manager import connector as _connector
from meho_backplane.connectors.sddc_manager import typed_reads as _typed_reads
from meho_backplane.connectors.sddc_manager.profile import SDDC_EXECUTION_PROFILE
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "sddc-manager-9.0"
_SPEC_FILE = "sddc-manager-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"


def _typed_read_paths() -> dict[str, str]:
    """The live ``_*_PATH`` constants of ``typed_reads``, by constant name."""
    return {
        name: value
        for name, value in vars(_typed_reads).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the sddc-manager connector dispatches."""
    declared = {f"GET:{path}" for path in _typed_read_paths().values()}
    declared.add(f"POST:{_connector._SESSION_CREATE_PATH}")
    declared.add(f"GET:{SDDC_EXECUTION_PROFILE.fingerprint.path}")
    return declared


def test_typed_read_path_constants_are_all_discovered() -> None:
    """Guard: the introspection finds every typed-read path constant.

    Pinning the exact constant-name set (the #2944 guard shape) means a
    renamed or dropped ``_*_PATH`` constant — or a new one that skips
    the ``_PATH`` suffix convention — can't silently shrink the
    reconciled set to a vacuous pass.
    """
    assert sorted(_typed_read_paths()) == [
        "_CLUSTERS_PATH",
        "_CREDENTIALS_PATH",
        "_DOMAINS_PATH",
        "_DOMAIN_STATUS_PATH",
        "_HOSTS_PATH",
        "_LICENSE_KEYS_PATH",
        "_NETWORK_POOLS_PATH",
        "_NETWORK_POOL_GET_PATH",
        "_NSXT_CLUSTERS_PATH",
        "_SDDC_MANAGERS_PATH",
        "_SYSTEM_PATH",
        "_TASKS_PATH",
        "_VCENTERS_PATH",
        "_VCF_SERVICES_PATH",
    ]


def test_fingerprint_probe_literal_is_covered_by_the_typed_constant() -> None:
    """Guard: the connector's inline fingerprint literal can't drift uncovered.

    ``SddcManagerConnector.fingerprint`` GETs ``"/v1/sddc-managers"`` as
    an inline literal (not introspectable), relying on the typed
    ``_SDDC_MANAGERS_PATH`` declaring the same string. This pins that
    value equality so an edit to either side surfaces here instead of
    silently dropping the probe path from the reconcile.
    """
    assert _typed_reads._SDDC_MANAGERS_PATH == "/v1/sddc-managers"


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned sddc-manager-9.0 spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
