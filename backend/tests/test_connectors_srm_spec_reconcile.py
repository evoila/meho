# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""srm spec-reconcile lane -- hand-coded SRM paths vs the pinned vendor file.

Every vendor call the srm connector makes is listed here with its method:

* the login, ``POST /api/rest/srm/v2/session``;
* the server summary, ``GET /api/rest/srm/v2/server/info`` (``srm.about``
  and the version read);
* the pairing, remote login, recovery plan, protection group, issue and
  history reads (:data:`~meho_backplane.connectors.srm.recovery_paths.VENDOR_CALLS`);
* the two API files ``srm.api_file.get`` reads
  (``/api/rest/srm/v2/open-api.yaml`` and
  ``/api/rest/configure/v2/open-api.yaml``) -- marked "API file path,
  not in the API file" and left out of the served check: the appliance
  serves them as files, the vendor file does not list them.

The vendor file lists its paths relative to ``/api/rest/srm/v2``, so the
lane strips that base before it compares. The 9.1 file
(``vcf-protection-and-recovery-9.1/recovery-srm-v2.yaml``) is pinned on
the spec shelf. The 8.8 and 9.0 files are added to :data:`_SPEC_FILES`
once the lab pins them with ``srm.api_file.get``.

A second lane checks the appliance setup operations (``srm.appliance.*``
and ``srm.storage_adapter.install``): every call in
:data:`~meho_backplane.connectors.srm.setup_api.SETUP_API_CALLS` against
the pinned 9.1 setup API file
(``vcf-protection-and-recovery-9.1/appliance-configure-v2.yaml``), whose
paths are relative to ``/api/rest/configure/v2``. The 8.8 and 9.0 setup
API files join :data:`_SETUP_SPEC_FILES` once they are pinned.
"""

from __future__ import annotations

import pytest

from meho_backplane.connectors.srm import SrmConnector
from meho_backplane.connectors.srm import api_file as _api_file_module
from meho_backplane.connectors.srm.recovery_paths import VENDOR_CALLS
from meho_backplane.connectors.srm.setup_api import SETUP_API_CALLS
from tests._spec_shelf import (
    assert_op_ids_served,
    openapi_served_op_ids,
    require_shelf_spec,
)

#: One ``(shelf directory, file)`` per SRM version with a pinned file.
_SPEC_FILES = (("vcf-protection-and-recovery-9.1", "recovery-srm-v2.yaml"),)

#: One ``(shelf directory, file)`` per version with a pinned setup API file.
_SETUP_SPEC_FILES = (("vcf-protection-and-recovery-9.1", "appliance-configure-v2.yaml"),)


def _declared_api_op_ids() -> set[str]:
    """The hand-coded API calls, relative to the API base, as ``METHOD:/path``."""
    base = SrmConnector.api_base
    calls = {"POST": SrmConnector.session_path, "GET": SrmConnector.info_path}
    login_and_info = {f"{method}:{path.removeprefix(base)}" for method, path in calls.items()}
    return login_and_info | {f"{method}:{path}" for method, path in VENDOR_CALLS}


def test_srm_hand_coded_op_id_manifest_is_pinned() -> None:
    """Pin the swept op_id set so drift cannot silently shrink the reconcile."""
    assert _declared_api_op_ids() == {
        "POST:/session",
        "GET:/server/info",
        "GET:/pairings",
        "POST:/pairings/{pairing_id}/remote-session",
        "GET:/pairings/{pairing_id}/remote-session",
        "GET:/pairings/{pairing_id}/recovery-management/plans",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/protection-groups",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/recovery-steps/{view_mode}",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/vms",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/vms/{vm_id}/recovery-settings",
        "GET:/pairings/{pairing_id}/protection-management/groups",
        "GET:/pairings/{pairing_id}/protection-management/groups/{group_id}/vms",
        "GET:/pairings/{pairing_id}/issues",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/issues",
        "GET:/pairings/{pairing_id}/protection-management/groups/{group_id}/issues",
        "GET:/pairings/{pairing_id}/srms/{srm_id}/issues",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/history-reports",
        "GET:/pairings/{pairing_id}/recovery-management/plans/{plan_id}/history-reports/{report_id}",
        "GET:/tasks/{taskId}",
    }
    assert set(_api_file_module.API_FILES.values()) == {
        "/api/rest/srm/v2/open-api.yaml",
        "/api/rest/configure/v2/open-api.yaml",
    }


@pytest.mark.parametrize(("spec_dir", "spec_file"), _SPEC_FILES)
def test_srm_hand_coded_api_paths_are_served_by_the_pinned_file(
    spec_dir: str, spec_file: str
) -> None:
    """Every hand-coded SRM API call is served by the pinned vendor file."""
    spec_path = require_shelf_spec(spec_dir, spec_file)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_api_op_ids(), served, spec_label=f"{spec_dir}/{spec_file}")


def _declared_setup_op_ids() -> set[str]:
    """The setup API calls, relative to ``/api/rest/configure/v2``, as ``METHOD:/path``."""
    return {f"{method}:{path}" for method, path in SETUP_API_CALLS}


def test_srm_setup_api_manifest_is_pinned() -> None:
    """Pin the setup API call set so drift cannot silently shrink the reconcile."""
    declared = _declared_setup_op_ids()
    assert len(declared) == len(SETUP_API_CALLS) == 23
    assert {
        "PUT:/appliance/configuration-manager/configuration",
        "POST:/appliance/certificates/server/actions/probe-ssl-connection",
        "POST:/appliance/configuration-manager/actions/list-vc-services",
        "POST:/storage-replication-adapters",
        "GET:/tasks/{task_id}",
    } <= declared


@pytest.mark.parametrize(("spec_dir", "spec_file"), _SETUP_SPEC_FILES)
def test_srm_setup_api_paths_are_served_by_the_pinned_file(spec_dir: str, spec_file: str) -> None:
    """Every setup API call of the appliance operations is served by the pinned file."""
    spec_path = require_shelf_spec(spec_dir, spec_file)
    served = openapi_served_op_ids(spec_path)
    assert_op_ids_served(_declared_setup_op_ids(), served, spec_label=f"{spec_dir}/{spec_file}")
