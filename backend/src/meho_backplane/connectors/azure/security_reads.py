# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure security and governance reads.

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.security.SecurityOps`. The operation
table, the group text and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.security`; how Azure's answers become
rows, and how parameters are checked, is in
:mod:`meho_backplane.connectors.azure.security_parse`.

Every function here only reads; nothing changes in Azure. Lists are read
with :func:`~meho_backplane.connectors.azure._paging.list_all_pages`, so
every ``nextLink`` is checked before the token goes to it and the paging
limits hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.adapters.http import json_payload_or_empty
from meho_backplane.connectors.azure._http import send
from meho_backplane.connectors.azure._paging import list_all_pages
from meho_backplane.connectors.azure._paths import (
    ACTIVITY_LOG_API_VERSION,
    ACTIVITY_LOG_PATH,
    AUTHORIZATION_API_VERSION,
    DEFENDER_PLANS_API_VERSION,
    DEFENDER_PLANS_PATH,
    POLICY_ASSIGNMENTS_API_VERSION,
    POLICY_ASSIGNMENTS_PATH,
    POLICY_INSIGHTS_API_VERSION,
    POLICY_STATES_SUMMARY_PATH,
    ROLE_ASSIGNMENTS_PATH,
    ROLE_DEFINITIONS_PATH,
    SECURE_SCORE_API_VERSION,
    SECURE_SCORE_CONTROLS_PATH,
    SECURE_SCORES_PATH,
    SECURITY_ALERTS_API_VERSION,
    SECURITY_ALERTS_PATH,
    SECURITY_ASSESSMENT_METADATA_API_VERSION,
    SECURITY_ASSESSMENT_METADATA_PATH,
    SECURITY_ASSESSMENTS_API_VERSION,
    SECURITY_ASSESSMENTS_PATH,
)
from meho_backplane.connectors.azure.security_parse import (
    activity_row,
    activity_window,
    alert_row,
    assessment_row,
    control_row,
    iso_time,
    list_answer,
    object_rows,
    parse_time,
    plan_row,
    policy_assignment_row,
    policy_summary,
    resource_group_name,
    role_assignment_row,
    role_names_by_id,
    role_scope,
    secure_score_row,
    severity_by_name,
    subscription_scope,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "ALERT_DEFAULT_DAYS",
    "activity_list_impl",
    "policy_assignment_list_impl",
    "policy_compliance_summary_impl",
    "role_assignment_list_impl",
    "security_alert_list_impl",
    "security_control_list_impl",
    "security_plan_list_impl",
    "security_recommendation_list_impl",
    "security_score_list_impl",
]

#: The days of ``azure.security.alert.list`` when the caller gives none.
ALERT_DEFAULT_DAYS: Final[int] = 7

#: Azure's only allowed summary resource of the policy states.
_LATEST: Final[str] = "latest"

#: The role definitions filter that also finds custom roles made below the scope.
_AT_SCOPE_AND_BELOW: Final[str] = "atScopeAndBelow()"


def _utc_now() -> datetime:
    """The current time in UTC. Tests replace it."""
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Defender for Cloud
# ----------------------------------------------------------------------


async def security_score_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.security.score.list``: the Defender for Cloud secure scores."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        SECURE_SCORES_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=SECURE_SCORE_API_VERSION,
    )
    return list_answer(paged, object_rows(paged.rows, secure_score_row))


async def security_control_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.security.control.list``: the secure score per control, with its recommendations."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        SECURE_SCORE_CONTROLS_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=SECURE_SCORE_API_VERSION,
        params={"$expand": "definition"},
    )
    return list_answer(paged, object_rows(paged.rows, control_row))


async def security_recommendation_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.security.recommendation.list``: the recommendations with one status.

    Azure's list has no status filter, so MEHO reads every page and keeps the
    rows with the asked status (``Unhealthy`` by default; ``All`` keeps every
    row). When a kept row has no severity, MEHO reads the recommendation
    metadata once and takes the severity from there.
    """
    wanted = str(params.get("status") or "Unhealthy")
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        SECURITY_ASSESSMENTS_PATH.format(scope=subscription_scope(config)),
        operator=operator,
        api_version=SECURITY_ASSESSMENTS_API_VERSION,
    )
    rows = object_rows(paged.rows, assessment_row)
    if wanted.lower() != "all":
        rows = [row for row in rows if (row["status"] or "").lower() == wanted.lower()]
    if any(row["severity"] is None for row in rows):
        metadata = await list_all_pages(
            connector,
            target,
            SECURITY_ASSESSMENT_METADATA_PATH.format(subscription_id=config.subscription_id),
            operator=operator,
            api_version=SECURITY_ASSESSMENT_METADATA_API_VERSION,
        )
        severities = severity_by_name(metadata.rows)
        for row in rows:
            if row["severity"] is None and row["name"]:
                row["severity"] = severities.get(row["name"].lower())
    return list_answer(paged, rows, status_filter=wanted)


async def security_alert_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.security.alert.list``: the alerts Defender made in the last ``days`` days.

    Azure's list has no time filter, so MEHO keeps the alerts whose time is
    inside the window. An alert without a readable time is kept, so nothing
    is left out without a sign.
    """
    days = params.get("days")
    if not isinstance(days, int) or isinstance(days, bool):
        days = ALERT_DEFAULT_DAYS
    config = resolve_target_config(target)
    now = _utc_now()
    since = now - timedelta(days=days)
    paged = await list_all_pages(
        connector,
        target,
        SECURITY_ALERTS_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=SECURITY_ALERTS_API_VERSION,
    )
    rows = []
    for row in object_rows(paged.rows, alert_row):
        made = parse_time(row["time"])
        if made is None or made >= since:
            rows.append(row)
    window = {"days": days, "from": iso_time(since), "to": iso_time(now)}
    return list_answer(paged, rows, window=window)


async def security_plan_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.security.plan.list``: the Defender plans and their tier."""
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        DEFENDER_PLANS_PATH.format(scope=subscription_scope(config)),
        operator=operator,
        api_version=DEFENDER_PLANS_API_VERSION,
    )
    return list_answer(paged, object_rows(paged.rows, plan_row))


# ----------------------------------------------------------------------
# Azure Policy
# ----------------------------------------------------------------------


async def policy_compliance_summary_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.policy.compliance.summary``: Azure Policy compliance of the subscription.

    The summarize call is a POST that only reads, so it is sent as a
    repeatable call (tried again after a 5xx, like a GET). Azure answers in
    one piece; there is no next page.
    """
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    response = await send(
        connector,
        target,
        "POST",
        POLICY_STATES_SUMMARY_PATH.format(
            subscription_id=config.subscription_id, summary_resource=_LATEST
        ),
        operator=operator,
        params={"api-version": POLICY_INSIGHTS_API_VERSION},
        repeatable=True,
    )
    return policy_summary(json_payload_or_empty(response))


async def policy_assignment_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.policy.assignment.list``: every policy assignment that applies to the subscription.

    No filter is sent, so the list holds the assignments on the
    subscription, those inherited from management groups above it, and
    those on resource groups and resources in it.
    """
    del params  # the operation takes no parameters
    config = resolve_target_config(target)
    paged = await list_all_pages(
        connector,
        target,
        POLICY_ASSIGNMENTS_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=POLICY_ASSIGNMENTS_API_VERSION,
    )
    return list_answer(paged, object_rows(paged.rows, policy_assignment_row))


# ----------------------------------------------------------------------
# Role assignments and the activity log
# ----------------------------------------------------------------------


async def role_assignment_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.role.assignment.list``: who has which role at which scope.

    Reads the role assignments of the checked scope, then the roles of the
    subscription once (with ``atScopeAndBelow()``, so custom roles made on a
    resource group are found too) to turn each role id into its name.
    Principals stay ids: their names live in Microsoft Entra ID.
    """
    config = resolve_target_config(target)
    scope = role_scope(params.get("scope"), config, target_name=target.name)
    paged = await list_all_pages(
        connector,
        target,
        ROLE_ASSIGNMENTS_PATH.format(scope=scope),
        operator=operator,
        api_version=AUTHORIZATION_API_VERSION,
    )
    roles = await list_all_pages(
        connector,
        target,
        ROLE_DEFINITIONS_PATH.format(scope=subscription_scope(config)),
        operator=operator,
        api_version=AUTHORIZATION_API_VERSION,
        params={"$filter": _AT_SCOPE_AND_BELOW},
    )
    names = role_names_by_id(roles.rows)
    rows = object_rows(paged.rows, lambda item: role_assignment_row(item, names))
    return list_answer(paged, rows, scope=f"/{scope}")


async def activity_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.activity.list``: the activity log events of a time window.

    Azure's filter takes the time window and, when given, the resource
    group; it allows no other condition. So ``caller`` and ``status`` are
    applied by MEHO to the rows it read, without regard to case.
    """
    config = resolve_target_config(target)
    window = activity_window(params, _utc_now(), target_name=target.name)
    resource_group = resource_group_name(params.get("resource_group"), target_name=target.name)
    clauses = [
        f"eventTimestamp ge '{iso_time(window.start)}'",
        f"eventTimestamp le '{iso_time(window.end)}'",
    ]
    if resource_group is not None:
        clauses.append(f"resourceGroupName eq '{resource_group}'")
    paged = await list_all_pages(
        connector,
        target,
        ACTIVITY_LOG_PATH.format(subscription_id=config.subscription_id),
        operator=operator,
        api_version=ACTIVITY_LOG_API_VERSION,
        params={"$filter": " and ".join(clauses)},
    )
    rows = object_rows(paged.rows, activity_row)
    for key in ("caller", "status"):
        wanted = params.get(key)
        if isinstance(wanted, str) and wanted:
            rows = [row for row in rows if (row[key] or "").casefold() == wanted.casefold()]
    return list_answer(paged, rows, window=window.as_dict())
