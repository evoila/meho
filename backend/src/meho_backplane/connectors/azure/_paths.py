# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files
(``backend/tests/test_connectors_azure_spec_reconcile.py``). A typo, a
renamed endpoint or a wrong API version then fails in CI and not on a
customer system. Later reads add their paths here, and each new ``*_PATH``
constant gets one line in that check.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations. Each
family keeps its paths in its own section below, so families built at the
same time do not edit the same lines.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "AUTOMATION_ACCOUNTS_PATH",
    "AUTOMATION_API_VERSION",
    "AUTOMATION_JOBS_PATH",
    "AUTOMATION_JOB_SCHEDULES_PATH",
    "AUTOMATION_RUNBOOKS_PATH",
    "AUTOMATION_SCHEDULES_PATH",
    "LOCATIONS_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "locations_path",
    "subscription_path",
]

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: ``GET /subscriptions/{subscriptionId}/locations``: the Azure regions of one
#: subscription (Subscriptions - List Locations).
LOCATIONS_PATH: Final[str] = "/subscriptions/{subscription_id}/locations"

#: The API version of the subscription and location reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"

# ---- aks (Task #5087) ----

# ---- arc (Task #5084) ----

# ---- automation (Task #5092) ----
#
# Azure Automation (Microsoft.Automation). MEHO reads only these lists: the
# Automation accounts, and per account its runbooks, jobs, schedules and job
# schedules (the links between a schedule and its runbooks). Paths that
# return runbook code, job output or streams, credentials or variables are
# never added here, because they can hold secrets; a test fails when one is.

#: The API version of the Azure Automation reads (Microsoft.Automation).
AUTOMATION_API_VERSION: Final[str] = "2024-10-23"

_AUTOMATION_ACCOUNT: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Automation/automationAccounts/{account}"
)

#: ``GET``: every Automation account of the subscription (AutomationAccount_List).
AUTOMATION_ACCOUNTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Automation/automationAccounts"
)

#: ``GET``: the runbooks of one account (Runbook_ListByAutomationAccount).
AUTOMATION_RUNBOOKS_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/runbooks"

#: ``GET``: the jobs of one account, narrowed with ``$filter``
#: (Job_ListByAutomationAccount).
AUTOMATION_JOBS_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/jobs"

#: ``GET``: the schedules of one account (Schedule_ListByAutomationAccount).
AUTOMATION_SCHEDULES_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/schedules"

#: ``GET``: the job schedules of one account, which link a schedule to a
#: runbook (JobSchedule_ListByAutomationAccount).
AUTOMATION_JOB_SCHEDULES_PATH: Final[str] = f"{_AUTOMATION_ACCOUNT}/jobSchedules"

# ---- avs (Task #5091) ----

# ---- compute (Task #5083) ----

# ---- cost (Task #5088) ----

# ---- inventory (Task #5082) ----

# ---- migrate (Task #5093) ----

# ---- monitor (Task #5090) ----

# ---- network (Task #5085) ----

# ---- security (Task #5089) ----

# ---- storage (Task #5086) ----


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


def locations_path(subscription_id: str) -> str:
    """Return the path of the regions of one subscription.

    *subscription_id* must already be checked as a GUID, like in
    :func:`subscription_path`.
    """
    return LOCATIONS_PATH.format(subscription_id=subscription_id)
