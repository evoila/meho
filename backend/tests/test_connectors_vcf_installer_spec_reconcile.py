# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vcf-installer real-spec reconcile lane — the #2980 harness.

Asserts every hand-coded ``METHOD:/path`` the vcf-installer connector dispatches
is served by **both** pinned installer specs on the shelf: ``vcf-installer-9.1``
and ``vcf-installer-9.0`` (``vmware/vcf-api-specs``' OpenAPI 3.0.1 documents;
provenance in each shelf folder's ``MANIFEST.md``). The connector advertises
``>=9.0,<10.0``, so a 9.0.x Installer resolves to it; the 9.0 lane is what
makes that range safe. A path that only 9.1 serves fails the 9.0 lane.
Parse-only set compare — no DB, no embeddings, no containers — so it lives in
the required unit sweep per
``docs/decisions/spec-reconcile-guards-standard.md``. Uniform skip when the shelf
is unconfigured (``tests/_spec_shelf.py`` contract).

The declared set is introspected from the connector's live constants (the #2944
pattern — never a hardcoded mirror), across the four surfaces that hand-code a
path:

* :mod:`~meho_backplane.connectors.vcf_installer.typed_reads` — the ``_*_PATH``
  module constants (the bring-up status and validation status polls, the
  id-less bring-up task list, and the depot-lifecycle reads). Each
  typed read dispatches through :meth:`HttpConnector._get_json`, so each
  declares ``GET:``.
* :mod:`~meho_backplane.connectors.vcf_installer.typed_writes` — the submit
  primitives' ``TYPED_WRITE_DECLARED_OP_IDS`` (``POST /v1/sddcs/validations``,
  ``POST /v1/sddcs``), which carry their own HTTP method.
* :mod:`~meho_backplane.connectors.vcf_installer.connector` — the token mint
  ``_SESSION_CREATE_PATH`` (``POST /v1/tokens``, sourced from the
  ``session_login_token`` scheme spec).
* :mod:`~meho_backplane.connectors.vcf_installer.profile` — the declarative
  fingerprint recipe's ``GET /v1/system/appliance-info``.
* :mod:`~meho_backplane.connectors.vcf_installer.system_settings` — the
  proxy, CEIP, system-setting, trusted-certificate-remove and vendor-job calls
  (``SYSTEM_DECLARED_OP_IDS``), checked against both files.
* :mod:`~meho_backplane.connectors.vcf_installer.resources_calc` — the
  resource calculation (``RESOURCES_DECLARED_OP_IDS``). Only the 9.1 file has
  it, so it is checked against 9.1, and a guard checks that 9.0 still lacks
  it (the operation stops early on a 9.0 target).
* :mod:`~meho_backplane.connectors.vcf_installer.bringup` — the governed
  composite's ``BRINGUP_DECLARED_OP_IDS`` (``POST /v1/sddcs/validations``,
  ``POST /v1/sddcs``, ``GET /v1/sddcs/validations/{id}``), which likewise
  carry their own ``METHOD:`` verb.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.vcf_installer import bringup as _bringup
from meho_backplane.connectors.vcf_installer import connector as _connector
from meho_backplane.connectors.vcf_installer import resources_calc as _resources_calc
from meho_backplane.connectors.vcf_installer import system_settings as _system_settings
from meho_backplane.connectors.vcf_installer import typed_reads as _typed_reads
from meho_backplane.connectors.vcf_installer import typed_writes as _typed_writes
from meho_backplane.connectors.vcf_installer.profile import INSTALLER_EXECUTION_PROFILE
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

_SPEC_DIRS = ("vcf-installer-9.0", "vcf-installer-9.1")
_SPEC_FILE = "vcf-installer-openapi.json"


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
    declared |= set(_typed_writes.TYPED_WRITE_DECLARED_OP_IDS)
    declared |= set(_bringup.BRINGUP_DECLARED_OP_IDS)
    declared |= set(_system_settings.SYSTEM_DECLARED_OP_IDS)
    return declared


def test_typed_read_path_constants_are_all_discovered() -> None:
    """Guard: the introspection finds every typed-read path constant.

    Pinning the exact constant-name set means a renamed or dropped ``_*_PATH``
    constant — or a new one that skips the suffix convention — can't silently
    shrink the reconciled set to a vacuous pass.
    """
    assert sorted(_typed_read_paths()) == [
        "_BUNDLES_PATH",
        "_BUNDLE_DOWNLOAD_STATUS_PATH",
        "_DEPOT_SETTINGS_PATH",
        "_DEPOT_SYNC_INFO_PATH",
        "_SDDC_LIST_PATH",
        "_SDDC_STATUS_PATH",
        "_TRUSTED_CERTIFICATES_PATH",
        "_VALIDATION_STATUS_PATH",
    ]


def test_typed_write_declared_op_ids_are_pinned() -> None:
    """Guard: the submit primitives' declared write set can't go vacuous.

    Pinning the exact ``METHOD:/path`` set means dropping or renaming a write
    path constant can't silently shrink the reconciled set to a passing subset.
    """
    assert (
        frozenset(
            {
                "POST:/v1/sddcs/validations",
                "POST:/v1/sddcs",
                "PATCH:/v1/sddcs/{id}",
                "PUT:/v1/system/settings/depot",
                "POST:/v1/sddc-manager/trusted-certificates",
                "PATCH:/v1/bundles/{id}",
            }
        )
        == _typed_writes.TYPED_WRITE_DECLARED_OP_IDS
    )


def test_bringup_declared_op_ids_are_pinned() -> None:
    """Guard: the composite's declared write/validate set can't go vacuous.

    Pinning the exact ``METHOD:/path`` set means dropping or renaming a bring-up
    path constant can't silently shrink the reconciled set to a passing subset.
    """
    assert (
        frozenset(
            {
                "POST:/v1/sddcs/validations",
                "POST:/v1/sddcs",
                "GET:/v1/sddcs/validations/{id}",
            }
        )
        == _bringup.BRINGUP_DECLARED_OP_IDS
    )


@pytest.mark.parametrize("spec_dir", _SPEC_DIRS)
def test_declared_op_ids_are_served_by_the_pinned_spec(spec_dir: str) -> None:
    """Every hand-coded op_id is served by the pinned 9.0 and 9.1 installer specs."""
    spec_path = require_shelf_spec(spec_dir, _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_op_ids(), served, spec_label=f"{spec_dir}/{_SPEC_FILE}")


def test_system_declared_op_ids_are_pinned() -> None:
    """Guard: the settings calls can't shrink to a passing subset."""
    assert (
        frozenset(
            {
                "GET:/v1/system/proxy-configuration",
                "PATCH:/v1/system/proxy-configuration",
                "GET:/v1/system/ceip",
                "PATCH:/v1/system/ceip",
                "GET:/v1/system",
                "PATCH:/v1/system",
                "DELETE:/v1/sddc-manager/trusted-certificates/{alias}",
                "GET:/v1/sddc-manager/trusted-certificates",
                "GET:/v1/system/settings/depot",
                "GET:/v1/tasks",
                "GET:/v1/tasks/{id}",
            }
        )
        == _system_settings.SYSTEM_DECLARED_OP_IDS
    )


def test_resources_declared_op_ids_are_pinned() -> None:
    """Guard: the 9.1-only resource calculation calls are pinned."""
    assert (
        frozenset(
            {
                "POST:/v1/sddcs/resources-calculation",
                "GET:/v1/sddcs/resources-calculation/{id}",
            }
        )
        == _resources_calc.RESOURCES_DECLARED_OP_IDS
    )


def test_resources_calculation_is_served_by_the_pinned_9_1_spec() -> None:
    """The 9.1 file serves the resource calculation calls."""
    spec_path = require_shelf_spec("vcf-installer-9.1", _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(
        set(_resources_calc.RESOURCES_DECLARED_OP_IDS),
        served,
        spec_label=f"vcf-installer-9.1/{_SPEC_FILE}",
    )


def test_resources_calculation_is_not_in_the_pinned_9_0_spec() -> None:
    """The 9.0 file lacks the calls, so the operation's version check is needed.

    When a newer 9.0 file adds them, change the operation's first version and
    this test together.
    """
    spec_path = require_shelf_spec("vcf-installer-9.0", _SPEC_FILE)
    served = openapi_served_op_ids(spec_path)
    assert not (set(_resources_calc.RESOURCES_DECLARED_OP_IDS) & set(served))
