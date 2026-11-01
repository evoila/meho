# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vendor paths of the SRM pairing, recovery plan, protection group, issue and history reads.

The paths are relative to the SRM API base (``/api/rest/srm/v2``). The
placeholder names are the vendor file's, so
``tests/test_connectors_srm_spec_reconcile.py`` checks each call in
:data:`VENDOR_CALLS` against the pinned file.
"""

from __future__ import annotations

from typing import Final

PAIRINGS: Final = "/pairings"
REMOTE_SESSION: Final = "/pairings/{pairing_id}/remote-session"
PLANS: Final = "/pairings/{pairing_id}/recovery-management/plans"
PLAN: Final = f"{PLANS}/{{plan_id}}"
PLAN_GROUPS: Final = f"{PLAN}/protection-groups"
PLAN_STEPS: Final = f"{PLAN}/recovery-steps/{{view_mode}}"
PLAN_VMS: Final = f"{PLAN}/vms"
PLAN_VM_SETTINGS: Final = f"{PLAN_VMS}/{{vm_id}}/recovery-settings"
GROUPS: Final = "/pairings/{pairing_id}/protection-management/groups"
GROUP_VMS: Final = f"{GROUPS}/{{group_id}}/vms"
PAIRING_ISSUES: Final = "/pairings/{pairing_id}/issues"
PLAN_ISSUES: Final = f"{PLAN}/issues"
GROUP_ISSUES: Final = f"{GROUPS}/{{group_id}}/issues"
SRM_ISSUES: Final = "/pairings/{pairing_id}/srms/{srm_id}/issues"
HISTORY_REPORTS: Final = f"{PLAN}/history-reports"
HISTORY_REPORT: Final = f"{HISTORY_REPORTS}/{{report_id}}"
TASK: Final = "/tasks/{taskId}"

#: Every vendor call these reads make, as ``(method, path)``.
VENDOR_CALLS: Final = (
    ("GET", PAIRINGS),
    ("POST", REMOTE_SESSION),
    ("GET", REMOTE_SESSION),
    ("GET", PLANS),
    ("GET", PLAN),
    ("GET", PLAN_GROUPS),
    ("GET", PLAN_STEPS),
    ("GET", PLAN_VMS),
    ("GET", PLAN_VM_SETTINGS),
    ("GET", GROUPS),
    ("GET", GROUP_VMS),
    ("GET", PAIRING_ISSUES),
    ("GET", PLAN_ISSUES),
    ("GET", GROUP_ISSUES),
    ("GET", SRM_ISSUES),
    ("GET", HISTORY_REPORTS),
    ("GET", HISTORY_REPORT),
    ("GET", TASK),
)
