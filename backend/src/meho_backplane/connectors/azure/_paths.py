# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files. That
check arrives with the pinned Azure spec files; from then on a typo or a
renamed endpoint fails in CI and not on a customer system. Later reads add
their paths here.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MIGRATE_API_VERSION",
    "MIGRATE_ASSESSED_MACHINES_PATH",
    "MIGRATE_ASSESSMENTS_PATH",
    "MIGRATE_ASSESSMENT_PATH",
    "MIGRATE_GROUPS_PATH",
    "MIGRATE_MACHINES_PATH",
    "MIGRATE_PROJECTS_PATH",
    "OFFAZURE_API_VERSION",
    "OFFAZURE_HYPERV_SITES_PATH",
    "OFFAZURE_IMPORT_SITES_PATH",
    "OFFAZURE_MASTER_SITES_PATH",
    "OFFAZURE_SERVER_SITES_PATH",
    "OFFAZURE_VMWARE_SITES_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "subscription_path",
]

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: The API version of the subscription reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


# ---- migrate (Task #5093) ----
#
# Azure Migrate has two providers. Microsoft.Migrate holds the assessment
# projects, their machines, groups, assessments and assessed machines.
# Microsoft.OffAzure holds the discovery sites: the places where the Azure
# Migrate appliance (VMware, Hyper-V, physical servers) or a CSV import put the
# machines it found. Each provider has its own API version.

#: The API version of the assessment project reads (Microsoft.Migrate).
MIGRATE_API_VERSION: Final[str] = "2024-01-15"

#: The API version of the discovery site reads (Microsoft.OffAzure).
OFFAZURE_API_VERSION: Final[str] = "2023-06-06"

_MIGRATE_PROJECT: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Migrate/assessmentProjects/{project}"
)

_OFFAZURE: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.OffAzure"
)

#: ``GET``: every assessment project of the subscription
#: (AssessmentProjectsOperations_ListBySubscription).
MIGRATE_PROJECTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Migrate/assessmentProjects"
)

#: ``GET``: the machines one assessment project knows (MachinesOperations_ListByAssessmentProject).
MIGRATE_MACHINES_PATH: Final[str] = f"{_MIGRATE_PROJECT}/machines"

#: ``GET``: the groups of one assessment project (GroupsOperations_ListByAssessmentProject).
MIGRATE_GROUPS_PATH: Final[str] = f"{_MIGRATE_PROJECT}/groups"

#: ``GET``: the assessments of one group (AssessmentsOperations_ListByGroup).
MIGRATE_ASSESSMENTS_PATH: Final[str] = f"{_MIGRATE_PROJECT}/groups/{{group}}/assessments"

#: ``GET``: one assessment (AssessmentsOperations_Get).
MIGRATE_ASSESSMENT_PATH: Final[str] = f"{MIGRATE_ASSESSMENTS_PATH}/{{assessment}}"

#: ``GET``: the assessed machines of one assessment
#: (AssessedMachinesOperations_ListByAssessment).
MIGRATE_ASSESSED_MACHINES_PATH: Final[str] = f"{MIGRATE_ASSESSMENT_PATH}/assessedMachines"

#: ``GET``: the VMware sites of a resource group (appliance discovery).
OFFAZURE_VMWARE_SITES_PATH: Final[str] = f"{_OFFAZURE}/vmwareSites"

#: ``GET``: the Hyper-V sites of a resource group (appliance discovery).
OFFAZURE_HYPERV_SITES_PATH: Final[str] = f"{_OFFAZURE}/hypervSites"

#: ``GET``: the physical server sites of a resource group (appliance discovery).
OFFAZURE_SERVER_SITES_PATH: Final[str] = f"{_OFFAZURE}/serverSites"

#: ``GET``: the import sites of a resource group (machines from a CSV import).
OFFAZURE_IMPORT_SITES_PATH: Final[str] = f"{_OFFAZURE}/importSites"

#: ``GET``: the master sites of a resource group (a master site links the other sites).
OFFAZURE_MASTER_SITES_PATH: Final[str] = f"{_OFFAZURE}/masterSites"
