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
same time do not edit the same lines. For the same reason the module has no
``__all__``: every family would add its names at the same place. The spec path
check finds the constants with ``vars()``.
"""

from __future__ import annotations

from typing import Final

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
#
# Azure Kubernetes Service (AKS): clusters, node pools, upgrade options and
# the Kubernetes versions per region. Reads only. MEHO never calls the
# actions that return a kubeconfig (a cluster login): listClusterUserCredential,
# listClusterAdminCredential, listClusterMonitoringUserCredential, and the
# listCredential actions of access profiles and managed namespaces.

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.ContainerService/managedClusters``:
#: every AKS cluster of the subscription (Managed Clusters - List).
AKS_CLUSTERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.ContainerService/managedClusters"
)

#: ``GET .../resourceGroups/{resourceGroupName}/providers/Microsoft.ContainerService``
#: ``/managedClusters/{resourceName}``: one AKS cluster (Managed Clusters - Get).
AKS_CLUSTER_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}"
)

#: ``GET .../managedClusters/{resourceName}/upgradeProfiles/default``: the
#: Kubernetes versions the cluster and its node pools can move to (Managed
#: Clusters - Get Upgrade Profile).
AKS_UPGRADE_PROFILE_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}/upgradeProfiles/default"
)

#: ``GET .../managedClusters/{resourceName}/agentPools``: the node pools of one
#: cluster; Azure calls them agent pools (Agent Pools - List).
AKS_NODE_POOLS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.ContainerService/managedClusters/{cluster}/agentPools"
)

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.ContainerService``
#: ``/locations/{location}/kubernetesVersions``: the Kubernetes versions AKS
#: offers in one region (Managed Clusters - List Kubernetes Versions).
AKS_KUBERNETES_VERSIONS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.ContainerService"
    "/locations/{location}/kubernetesVersions"
)

#: The API version of every AKS read (Microsoft.ContainerService).
AKS_API_VERSION: Final[str] = "2026-08-01"

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
#
# Azure VMware Solution: private clouds, their clusters and hosts, and the host
# quota in a region. Reads only. MEHO never calls the actions that return the
# vCenter and NSX passwords (listAdminCredentials) or license keys.

#: The API version of the Azure VMware Solution reads (Microsoft.AVS).
AVS_API_VERSION: Final[str] = "2026-03-01"

#: ``GET .../providers/Microsoft.AVS/privateClouds``: every Azure VMware
#: Solution private cloud of the subscription (Private Clouds - List In
#: Subscription).
AVS_PRIVATE_CLOUDS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.AVS/privateClouds"
)

#: ``GET .../privateClouds/{privateCloudName}/clusters``: the clusters of one
#: private cloud, without the management cluster (Clusters - List).
AVS_CLUSTERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.AVS/privateClouds/{private_cloud}/clusters"
)

#: ``GET .../clusters/{clusterName}/hosts``: the hosts of one cluster of a
#: private cloud (Hosts - List).
AVS_HOSTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.AVS/privateClouds/{private_cloud}/clusters/{cluster}/hosts"
)

#: ``POST .../providers/Microsoft.AVS/locations/{location}/checkQuotaAvailability``:
#: the host quota of the subscription in one region (Locations - Check Quota
#: Availability). A read, even though it is a POST.
AVS_QUOTA_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.AVS/locations/{location}"
    "/checkQuotaAvailability"
)

# ---- compute (Task #5083) ----
#
# Three Microsoft.Compute API versions, one per pinned spec file: VMs, images
# and scale sets (ComputeRP), managed disks (DiskRP), and the VM sizes per
# region (Resource SKUs).

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/virtualMachines``:
#: every VM of the subscription (Virtual Machines - List All). With
#: ``statusOnly=true`` the same call returns the run-time state of each VM.
VIRTUAL_MACHINES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines"
)

#: ``GET .../resourceGroups/{resourceGroupName}/providers/Microsoft.Compute/virtualMachines``:
#: the VMs of one resource group (Virtual Machines - List).
RESOURCE_GROUP_VIRTUAL_MACHINES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachines"
)

#: ``GET .../virtualMachines/{vmName}``: one VM (Virtual Machines - Get). With
#: ``$expand=instanceView`` the answer also holds its run-time state.
VIRTUAL_MACHINE_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachines/{vm_name}"
)

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/images``:
#: the managed images of the subscription (Images - List).
IMAGES_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/images"

#: The managed images of one resource group (Images - List By Resource Group).
RESOURCE_GROUP_IMAGES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/images"
)

#: ``GET .../providers/Microsoft.Compute/virtualMachineScaleSets``: the scale
#: sets of the subscription (Virtual Machine Scale Sets - List All).
SCALE_SETS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachineScaleSets"
)

#: The scale sets of one resource group (Virtual Machine Scale Sets - List).
RESOURCE_GROUP_SCALE_SETS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/virtualMachineScaleSets"
)

#: The API version of the VM, image and scale-set reads (Microsoft.Compute).
COMPUTE_API_VERSION: Final[str] = "2026-04-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/disks``:
#: the managed disks of the subscription (Disks - List).
DISKS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/disks"

#: The managed disks of one resource group (Disks - List By Resource Group).
RESOURCE_GROUP_DISKS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Compute/disks"
)

#: The API version of the managed-disk reads (Microsoft.Compute disks).
DISKS_API_VERSION: Final[str] = "2026-03-02"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Compute/skus``:
#: the compute sizes the subscription can see (Resource Skus - List). Only the
#: filter ``location eq '<region>'`` works on this call.
COMPUTE_SKUS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus"

#: The API version of the size list. The Resource SKUs call has its own API
#: version, older than the one of the VM reads.
COMPUTE_SKUS_API_VERSION: Final[str] = "2021-07-01"

# ---- cost (Task #5088) ----

#: ``POST /{scope}/providers/Microsoft.CostManagement/query``: the cost of a
#: subscription or a resource group (Query - Usage). A read, even though it is
#: a POST. ``scope`` is ``subscriptions/{id}`` or
#: ``subscriptions/{id}/resourceGroups/{name}``, without a leading slash.
COST_QUERY_PATH: Final[str] = "/{scope}/providers/Microsoft.CostManagement/query"

#: ``POST /{scope}/providers/Microsoft.CostManagement/forecast``: the expected
#: cost of a scope (Forecast - Usage). A read, even though it is a POST.
COST_FORECAST_PATH: Final[str] = "/{scope}/providers/Microsoft.CostManagement/forecast"

#: The API version of the cost query and the forecast (Microsoft.CostManagement).
COST_MANAGEMENT_API_VERSION: Final[str] = "2026-06-01"

#: ``GET /{scope}/providers/Microsoft.Consumption/budgets``: the budgets of a
#: subscription or a resource group (Budgets - List).
BUDGETS_PATH: Final[str] = "/{scope}/providers/Microsoft.Consumption/budgets"

#: The API version of the budget reads (Microsoft.Consumption).
CONSUMPTION_API_VERSION: Final[str] = "2026-06-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Advisor/recommendations``:
#: Azure Advisor's recommendations for one subscription (Recommendations - List).
ADVISOR_RECOMMENDATIONS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations"
)

#: The API version of the Advisor reads (Microsoft.Advisor).
ADVISOR_API_VERSION: Final[str] = "2026-10-01"

#: The public Azure Retail Prices API. It is not Azure Resource Manager: it
#: needs no login and gets no token. Microsoft publishes no API description
#: file for it, so it is a full URL and not a ``*_PATH`` constant, and the spec
#: path check leaves it out.
RETAIL_PRICES_URL: Final[str] = "https://prices.azure.com/api/retail/prices"

#: The API version of the price list. From this version on, filter values must
#: match in upper and lower case.
RETAIL_PRICES_API_VERSION: Final[str] = "2023-01-01-preview"

# ---- inventory (Task #5082) ----

#: ``GET /subscriptions``: every subscription the login can see (Subscriptions -
#: List). Sent with :data:`SUBSCRIPTIONS_API_VERSION`.
SUBSCRIPTIONS_PATH: Final[str] = "/subscriptions"

#: ``GET /subscriptions/{subscriptionId}/resourcegroups`` (Resource Groups - List).
RESOURCE_GROUPS_PATH: Final[str] = "/subscriptions/{subscription_id}/resourcegroups"

#: ``GET /subscriptions/{subscriptionId}/resources`` (Resources - List).
RESOURCES_PATH: Final[str] = "/subscriptions/{subscription_id}/resources"

#: ``GET /subscriptions/{subscriptionId}/resourceGroups/{resourceGroupName}/resources``
#: (Resources - List By Resource Group).
RESOURCE_GROUP_RESOURCES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}/resources"
)

#: ``GET /subscriptions/{subscriptionId}/tagNames``: tag names, their values and
#: counts (Tags - List).
TAG_NAMES_PATH: Final[str] = "/subscriptions/{subscription_id}/tagNames"

#: ``GET`` and ``PATCH /{scope}/providers/Microsoft.Resources/tags/default``: the
#: tags of one resource (Tags - Get At Scope, Tags - Update At Scope). *scope* is
#: a resource ID without its first ``/``.
TAGS_AT_SCOPE_PATH: Final[str] = "/{scope}/providers/Microsoft.Resources/tags/default"

#: The API version of the resource, resource group and tag calls
#: (Microsoft.Resources).
RESOURCES_API_VERSION: Final[str] = "2021-04-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Authorization/locks``
#: (Management Locks - List At Subscription Level).
LOCKS_PATH: Final[str] = "/subscriptions/{subscription_id}/providers/Microsoft.Authorization/locks"

#: The API version of the lock reads (Microsoft.Authorization).
LOCKS_API_VERSION: Final[str] = "2020-05-01"

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

# ---- monitor (Task #5090) ----

# ---- network (Task #5085) ----

# ---- security (Task #5089) ----
#
# Microsoft Defender for Cloud, Azure Policy, role assignments and the
# activity log. Every call here only reads. A ``{scope}`` path takes the scope
# without its first slash, for example ``subscriptions/<id>`` or
# ``subscriptions/<id>/resourceGroups/<name>``; the caller checks that the
# scope stays inside the target's subscription.

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Security/secureScores``:
#: the Defender for Cloud secure scores (Secure Scores - List).
SECURE_SCORES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Security/secureScores"
)

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Security/secureScoreControls``:
#: the controls of the secure score (Secure Score Controls - List).
SECURE_SCORE_CONTROLS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Security/secureScoreControls"
)

#: The API version of the secure score reads (Microsoft.Security).
SECURE_SCORE_API_VERSION: Final[str] = "2020-01-01"

#: ``GET /{scope}/providers/Microsoft.Security/assessments``: the security
#: recommendations, one row per resource and recommendation (Assessments - List).
SECURITY_ASSESSMENTS_PATH: Final[str] = "/{scope}/providers/Microsoft.Security/assessments"

#: The API version of the recommendation list (Microsoft.Security).
SECURITY_ASSESSMENTS_API_VERSION: Final[str] = "2020-01-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Security/assessmentMetadata``:
#: the name and severity of every kind of recommendation (Assessments Metadata -
#: List By Subscription).
SECURITY_ASSESSMENT_METADATA_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Security/assessmentMetadata"
)

#: The API version of the recommendation metadata (Microsoft.Security).
SECURITY_ASSESSMENT_METADATA_API_VERSION: Final[str] = "2025-05-04"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Security/alerts``:
#: the Defender for Cloud security alerts (Alerts - List).
SECURITY_ALERTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Security/alerts"
)

#: The API version of the alert list (Microsoft.Security).
SECURITY_ALERTS_API_VERSION: Final[str] = "2022-01-01"

#: ``GET /{scopeId}/providers/Microsoft.Security/pricings``: the Defender
#: plans and whether each one is on (Pricings - List).
DEFENDER_PLANS_PATH: Final[str] = "/{scope}/providers/Microsoft.Security/pricings"

#: The API version of the Defender plan list (Microsoft.Security).
DEFENDER_PLANS_API_VERSION: Final[str] = "2024-01-01"

#: ``POST /subscriptions/{subscriptionId}/providers/Microsoft.PolicyInsights/
#: policyStates/{policyStatesSummaryResource}/summarize``: the policy
#: compliance summary (Policy States - Summarize For Subscription). A read,
#: even though it is a POST. Azure allows only ``latest`` as the summary
#: resource.
POLICY_STATES_SUMMARY_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.PolicyInsights"
    "/policyStates/{summary_resource}/summarize"
)

#: The API version of the policy compliance reads (Microsoft.PolicyInsights).
POLICY_INSIGHTS_API_VERSION: Final[str] = "2024-10-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Authorization/policyAssignments``:
#: every policy assignment that applies to the subscription (Policy
#: Assignments - List).
POLICY_ASSIGNMENTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Authorization/policyAssignments"
)

#: The API version of the policy assignment list (Microsoft.Authorization).
POLICY_ASSIGNMENTS_API_VERSION: Final[str] = "2026-07-01"

#: ``GET /{scope}/providers/Microsoft.Authorization/roleAssignments``: the
#: role assignments that apply to a scope (Role Assignments - List For Scope).
ROLE_ASSIGNMENTS_PATH: Final[str] = "/{scope}/providers/Microsoft.Authorization/roleAssignments"

#: ``GET /{scope}/providers/Microsoft.Authorization/roleDefinitions``: the
#: roles that can be assigned at a scope, to turn role ids into role names
#: (Role Definitions - List).
ROLE_DEFINITIONS_PATH: Final[str] = "/{scope}/providers/Microsoft.Authorization/roleDefinitions"

#: The API version of the role reads (Microsoft.Authorization).
AUTHORIZATION_API_VERSION: Final[str] = "2022-04-01"

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Insights/eventtypes/
#: management/values``: the activity log of the subscription (Activity Logs - List).
ACTIVITY_LOG_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Insights/eventtypes/management/values"
)

#: The API version of the activity log (Microsoft.Insights).
ACTIVITY_LOG_API_VERSION: Final[str] = "2015-04-01"

# ---- storage (Task #5086) ----
#
# Two hosts. The account and container lists go to Resource Manager, like
# every path above. The blob paths (``BLOB_LIST_PATH`` and ``BLOB_PATH``) are
# data-plane paths: they go to the storage account's own blob host,
# ``https://<account>.blob.core.windows.net``, which takes a token for Azure
# Storage and answers in XML. A data-plane path takes the Blob service
# version in the ``x-ms-version`` header, not as ``api-version``.

#: ``GET /subscriptions/{subscriptionId}/providers/Microsoft.Storage/storageAccounts``:
#: the storage accounts of one subscription (Storage Accounts - List).
STORAGE_ACCOUNTS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Storage/storageAccounts"
)

#: ``GET .../storageAccounts/{accountName}/blobServices/default/containers``:
#: the containers of one storage account, read on Resource Manager (Blob
#: Containers - List). The Reader role is enough.
BLOB_CONTAINERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/"
    "Microsoft.Storage/storageAccounts/{account}/blobServices/default/containers"
)

#: The API version of the storage reads on Resource Manager (Microsoft.Storage).
STORAGE_API_VERSION: Final[str] = "2026-09-01"

#: ``GET /{containerName}?restype=container&comp=list`` on the account's blob
#: host: the files (blobs) of one container (List Blobs). The query part is
#: fixed; MEHO sends it with the other query values.
BLOB_LIST_PATH: Final[str] = "/{container}?restype=container&comp=list"

#: ``/{containerName}/{blob}`` on the account's blob host: ``HEAD`` reads one
#: file's properties (Get Blob Properties), ``GET`` its bytes (Get Blob).
BLOB_PATH: Final[str] = "/{container}/{blob}"

#: The Blob service version of the data-plane calls, sent as ``x-ms-version``.
#: The pinned spec files also have 2026-12-06, a date after this code was
#: written; the lab proof checks which version the lab account accepts.
BLOB_API_VERSION: Final[str] = "2026-10-06"


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
