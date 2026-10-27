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
    "ACTIVITY_LOG_API_VERSION",
    "ACTIVITY_LOG_PATH",
    "AUTHORIZATION_API_VERSION",
    "DEFENDER_PLANS_API_VERSION",
    "DEFENDER_PLANS_PATH",
    "POLICY_ASSIGNMENTS_API_VERSION",
    "POLICY_ASSIGNMENTS_PATH",
    "POLICY_INSIGHTS_API_VERSION",
    "POLICY_STATES_SUMMARY_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "ROLE_ASSIGNMENTS_PATH",
    "ROLE_DEFINITIONS_PATH",
    "SECURE_SCORES_PATH",
    "SECURE_SCORE_API_VERSION",
    "SECURE_SCORE_CONTROLS_PATH",
    "SECURITY_ALERTS_API_VERSION",
    "SECURITY_ALERTS_PATH",
    "SECURITY_ASSESSMENTS_API_VERSION",
    "SECURITY_ASSESSMENTS_PATH",
    "SECURITY_ASSESSMENT_METADATA_API_VERSION",
    "SECURITY_ASSESSMENT_METADATA_PATH",
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
