# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-installer real-spec reconcile lane — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the vcf-installer connector dispatches
is served by the pinned ``vcf-installer-9.1`` spec on the shelf
(``vmware/vcf-api-specs``' OpenAPI 3.0.1 document; provenance in the shelf's
``MANIFEST.md``). Parse-only set compare — no DB, no embeddings, no containers —
so it lives in the required unit sweep per
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the shelf
is unconfigured (``tests/_spec_shelf.py`` contract).

The declared set is introspected from the connector's live constants (the #2944
pattern — never a hardcoded mirror), across the three surfaces that hand-code a
path:

* :mod:`~meho_backplane.connectors.vcf_installer.typed_reads` — the ``_*_PATH``
  module constants (currently the bring-up status poll). Each typed read
  dispatches through :meth:`HttpConnector._get_json`, so each declares ``GET:``.
* :mod:`~meho_backplane.connectors.vcf_installer.connector` — the token mint
  ``_SESSION_CREATE_PATH`` (``POST /v1/tokens``, sourced from the
  ``session_login_token`` scheme spec).
* :mod:`~meho_backplane.connectors.vcf_installer.profile` — the declarative
  fingerprint recipe's ``GET /v1/system/appliance-info``.

The governed bring-up composite's write paths (``POST /v1/sddcs``,
``POST /v1/sddcs/validations``) join this set when that increment lands.
"""

from __future__ import annotations

from meho_backplane.connectors.vcf_installer import connector as _connector
from meho_backplane.connectors.vcf_installer import typed_reads as _typed_reads
from meho_backplane.connectors.vcf_installer.profile import INSTALLER_EXECUTION_PROFILE
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIR = "vcf-installer-9.1"
_SPEC_FILE = "vcf-installer-openapi.json"
_SPEC_LABEL = f"{_SPEC_DIR}/{_SPEC_FILE}"


def _typed_read_paths() -> dict[str, str]:
    """The live ``_*_PATH`` constants of ``typed_reads``, by constant name."""
    return {
        name: value
        for name, value in vars(_typed_reads).items()
        if name.endswith("_PATH") and isinstance(value, str) and value.startswith("/")
    }


def _declared_op_ids() -> set[str]:
    """Every hand-coded ``METHOD:/path`` the vcf-installer connector dispatches."""
    declared = {f"GET:{path}" for path in _typed_read_paths().values()}
    declared.add(f"POST:{_connector._SESSION_CREATE_PATH}")
    declared.add(f"GET:{INSTALLER_EXECUTION_PROFILE.fingerprint.path}")
    return declared


def test_typed_read_path_constants_are_all_discovered() -> None:
    """Guard: the introspection finds every typed-read path constant.

    Pinning the exact constant-name set means a renamed or dropped ``_*_PATH``
    constant — or a new one that skips the suffix convention — can't silently
    shrink the reconciled set to a vacuous pass.
    """
    assert sorted(_typed_read_paths()) == ["_SDDC_STATUS_PATH"]


def test_declared_op_ids_are_served_by_the_pinned_spec() -> None:
    """Every hand-coded op_id is served by the pinned vcf-installer-9.1 spec."""
    spec_path = require_shelf_spec(_SPEC_DIR, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=_SPEC_LABEL)
