# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Azure Migrate reads (group ``migrate``).

Each ``*_impl`` function is the body behind a thin method of
:class:`~meho_backplane.connectors.azure.migrate.MigrateOps`. The operation
table and the guidance for the AI are in
:mod:`meho_backplane.connectors.azure.migrate`; the row shapes and the name
checks are in :mod:`meho_backplane.connectors.azure.migrate_rows`.

Azure Migrate keeps its data with two providers:

* ``Microsoft.Migrate``: assessment projects, the machines a project knows,
  groups of machines, assessments and the assessed machines of an assessment.
* ``Microsoft.OffAzure``: discovery sites. The Azure Migrate appliance puts
  the machines it finds into VMware, Hyper-V or physical server sites. A CSV
  import puts them into an import site.

Every read returns the same shape::

    {"value": [...rows...], "truncated": false,
     "omissions": {"count": 0, "items": []}, ...count blocks}

``value`` holds the rows. ``truncated`` is true when rows are missing, and
``omissions`` says what is missing and why (``what``, ``reason``,
``detail``). The count blocks are objects, so the ``result_objects`` hint of
each operation keeps them next to the stored-result link when the rows go
behind one. Every list follows ``nextLink`` with the shared paging helper and
its limits.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import (
    AzureApiError,
    AzureForbiddenError,
    AzureNotFoundError,
    AzureProviderNotRegisteredError,
    AzureThrottledError,
    AzureUrlRefusedError,
)
from meho_backplane.connectors.azure._paging import MAX_SECONDS, PagedRows, list_all_pages
from meho_backplane.connectors.azure._paths import (
    MIGRATE_API_VERSION,
    MIGRATE_ASSESSED_MACHINES_PATH,
    MIGRATE_ASSESSMENT_PATH,
    MIGRATE_ASSESSMENTS_PATH,
    MIGRATE_GROUPS_PATH,
    MIGRATE_MACHINES_PATH,
    MIGRATE_PROJECTS_PATH,
    OFFAZURE_API_VERSION,
    OFFAZURE_HYPERV_SITES_PATH,
    OFFAZURE_IMPORT_SITES_PATH,
    OFFAZURE_MASTER_SITES_PATH,
    OFFAZURE_SERVER_SITES_PATH,
    OFFAZURE_VMWARE_SITES_PATH,
)
from meho_backplane.connectors.azure.migrate_rows import (
    assessed_machine_row,
    assessment_row,
    group_row,
    is_migrate_name,
    machine_row,
    migrate_name_param,
    project_params,
    project_row,
    readiness_counts,
    resource_group_param,
    site_row,
    sort_by_source_and_name,
    source_counts,
)
from meho_backplane.connectors.azure.session import AzureTargetLike, resolve_target_config

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "MAX_GROUPS",
    "SITE_LISTS",
    "assessment_list_impl",
    "assessment_machine_list_impl",
    "assessment_params",
    "group_list_impl",
    "machine_list_impl",
    "project_list_impl",
    "read_assessment",
    "site_list_impl",
]

#: Most groups whose assessments one ``azure.migrate.assessment.list`` reads.
MAX_GROUPS: Final[int] = 50

#: Clock. Tests replace it to move time forward.
_monotonic = time.monotonic

#: The five site kinds and the path that lists each of them.
SITE_LISTS: Final[tuple[tuple[str, str], ...]] = (
    ("vmware", OFFAZURE_VMWARE_SITES_PATH),
    ("hyperv", OFFAZURE_HYPERV_SITES_PATH),
    ("server", OFFAZURE_SERVER_SITES_PATH),
    ("import", OFFAZURE_IMPORT_SITES_PATH),
    ("master", OFFAZURE_MASTER_SITES_PATH),
)

#: Errors that stop one part of a read made of several parts (one site kind,
#: one group). The other parts still count, and this part becomes an
#: omission. A login error is not in this list: it stops the whole read.
_PART_ERRORS: Final[tuple[type[Exception], ...]] = (
    AzureApiError,
    AzureNotFoundError,
    AzureUrlRefusedError,
    httpx.HTTPError,
)


def assessment_params(params: Mapping[str, Any]) -> dict[str, str]:
    """The checked ``resource_group``, ``project``, ``group`` and ``assessment``."""
    return {
        **project_params(params),
        "group": migrate_name_param(params, "group"),
        "assessment": migrate_name_param(params, "assessment"),
    }


def _reason(exc: Exception) -> str:
    if isinstance(exc, AzureForbiddenError):
        return "no_permission"
    if isinstance(exc, AzureNotFoundError):
        return "not_found"
    if isinstance(exc, AzureProviderNotRegisteredError):
        return "provider_not_registered"
    if isinstance(exc, AzureThrottledError):
        return "throttled"
    return "read_failed"


def _detail(exc: Exception) -> str:
    """MEHO's own text about the error. A lost connection gets a fixed text."""
    if isinstance(exc, httpx.HTTPError):
        return f"Azure did not answer ({type(exc).__name__}). Try again later."
    return str(exc)


def _paging_omission(what: str, paged: PagedRows) -> dict[str, Any] | None:
    if paged.omission is None:
        return None
    return {
        "what": what,
        "reason": str(paged.omission.get("kind")),
        "detail": str(paged.omission.get("message")),
    }


class _Reader:
    """Reads the lists of one call and keeps what was left out.

    A read made of several parts stops starting new parts after
    :data:`~meho_backplane.connectors.azure._paging.MAX_SECONDS` seconds, the
    same limit as one list; each part not read is named in the omissions.
    """

    def __init__(
        self, connector: AzureConnector, operator: Operator, target: AzureTargetLike
    ) -> None:
        self.connector = connector
        self.operator = operator
        self.target = target
        self.subscription_id = resolve_target_config(target).subscription_id
        self.omissions: list[dict[str, Any]] = []
        self.errors: list[Exception] = []
        self.started = _monotonic()

    def path(self, template: str, **names: str) -> str:
        """Fill *template* with the target's subscription and the checked *names*."""
        return template.format(subscription_id=self.subscription_id, **names)

    def omit(self, what: str, reason: str, detail: str) -> None:
        self.omissions.append({"what": what, "reason": reason, "detail": detail})

    async def rows(self, path: str, api_version: str, what: str) -> list[Mapping[str, Any]]:
        """Every row of one list. A limit becomes an omission; an error is raised."""
        paged = await list_all_pages(
            self.connector, self.target, path, operator=self.operator, api_version=api_version
        )
        omission = _paging_omission(what, paged)
        if omission is not None:
            self.omissions.append(omission)
        return [item for item in paged.rows if isinstance(item, Mapping)]

    async def part(self, path: str, api_version: str, what: str) -> list[Mapping[str, Any]] | None:
        """Like :meth:`rows`, but an error or the time limit becomes an omission and ``None``."""
        if _monotonic() - self.started >= MAX_SECONDS:
            self.omit(
                what,
                "time_limit",
                f"MEHO stopped starting new reads after {MAX_SECONDS:g} seconds, so this "
                "part was not read. Ask for it on its own.",
            )
            return None
        try:
            return await self.rows(path, api_version, what)
        except _PART_ERRORS as exc:
            self.errors.append(exc)
            self.omit(what, _reason(exc), _detail(exc))
            return None

    def raise_when_every_part_failed(self, parts: int) -> None:
        """Raise the first error when no part could be read, so the caller gets it as it is."""
        if parts and len(self.errors) >= parts:
            raise self.errors[0]

    def result(self, rows: list[dict[str, Any]], **blocks: Any) -> dict[str, Any]:
        items = list(self.omissions)
        return {
            "value": rows,
            "truncated": bool(items),
            "omissions": {"count": len(items), "items": items},
            **blocks,
        }


async def read_assessment(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Read one assessment (``resource_group``, ``project``, ``group``, ``assessment``).

    Returns :func:`~meho_backplane.connectors.azure.migrate_rows.assessment_row`.
    A write that creates an assessment can call this to follow it until
    Azure has computed it.
    """
    names = assessment_params(params)
    path = _Reader(connector, operator, target).path(MIGRATE_ASSESSMENT_PATH, **names)
    payload = await connector.arm_get(
        target, path, operator=operator, api_version=MIGRATE_API_VERSION
    )
    return assessment_row(payload, group=names["group"])


async def project_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.project.list``: every assessment project of the subscription."""
    del params  # the operation takes no parameters
    reader = _Reader(connector, operator, target)
    items = await reader.rows(
        reader.path(MIGRATE_PROJECTS_PATH), MIGRATE_API_VERSION, "assessment projects"
    )
    return reader.result([project_row(item) for item in items])


async def site_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.site.list``: the five kinds of discovery site of one resource group.

    One list per kind. A kind that cannot be read becomes an omission and
    its count is ``None`` (not known), unless no kind could be read at all:
    then the first error is raised.
    """
    resource_group = resource_group_param(params)
    reader = _Reader(connector, operator, target)
    rows: list[dict[str, Any]] = []
    kind_counts: dict[str, int | None] = {}
    for kind, template in SITE_LISTS:
        items = await reader.part(
            reader.path(template, resource_group=resource_group),
            OFFAZURE_API_VERSION,
            f"{kind} sites",
        )
        kind_counts[kind] = None if items is None else len(items)
        rows.extend(site_row(item, kind) for item in items or ())
    reader.raise_when_every_part_failed(len(SITE_LISTS))
    return reader.result(rows, kind_counts=kind_counts)


async def machine_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.machine.list``: the machines of one assessment project, by source."""
    names = project_params(params)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(
        reader.path(MIGRATE_MACHINES_PATH, **names), MIGRATE_API_VERSION, "machines"
    )
    rows = sort_by_source_and_name(machine_row(item) for item in items)
    return reader.result(rows, source_counts=source_counts(rows))


async def group_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.group.list``: the groups of one assessment project."""
    names = project_params(params)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(
        reader.path(MIGRATE_GROUPS_PATH, **names), MIGRATE_API_VERSION, "groups"
    )
    return reader.result([group_row(item) for item in items])


def _usable_group_names(reader: _Reader, items: list[Mapping[str, Any]]) -> list[str]:
    """The group names MEHO may put into a path, at most :data:`MAX_GROUPS`.

    Group names come from Azure, so they are checked like a caller's name.
    """
    names: list[str] = []
    for item in items:
        name = item.get("name")
        if isinstance(name, str) and is_migrate_name(name):
            names.append(name)
            continue
        reader.omit(
            "the assessments of one group",
            "name_not_usable",
            "Azure returned a group name that MEHO does not put into a path, "
            "so the assessments of that group were not read.",
        )
    if len(names) > MAX_GROUPS:
        reader.omit(
            "groups",
            "group_limit",
            f"MEHO read the assessments of the first {MAX_GROUPS} of {len(names)} "
            "groups. Pass group to read the other groups one at a time.",
        )
    return names[:MAX_GROUPS]


async def assessment_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.assessment.list``: the assessments of one group, or of every group.

    Azure lists assessments per group only. Without ``group``, MEHO lists the
    groups first and then reads each group. A group that cannot be read
    becomes an omission, unless no group could be read at all.
    """
    names = project_params(params)
    reader = _Reader(connector, operator, target)
    if params.get("group") is not None:
        groups = [migrate_name_param(params, "group")]
    else:
        items = await reader.rows(
            reader.path(MIGRATE_GROUPS_PATH, **names), MIGRATE_API_VERSION, "groups"
        )
        groups = _usable_group_names(reader, items)
    rows: list[dict[str, Any]] = []
    for group in groups:
        found = await reader.part(
            reader.path(MIGRATE_ASSESSMENTS_PATH, group=group, **names),
            MIGRATE_API_VERSION,
            f"assessments of group {group}",
        )
        rows.extend(assessment_row(item, group=group) for item in found or ())
    reader.raise_when_every_part_failed(len(groups))
    return reader.result(rows)


async def assessment_machine_list_impl(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``azure.migrate.assessment.machine.list``: the assessed machines of one assessment.

    Reads the assessment first, so the costs come with their currency and
    the settings they depend on, then every assessed machine.
    """
    names = assessment_params(params)
    assessment = await read_assessment(connector, operator, target, params)
    reader = _Reader(connector, operator, target)
    items = await reader.rows(
        reader.path(MIGRATE_ASSESSED_MACHINES_PATH, **names),
        MIGRATE_API_VERSION,
        "assessed machines",
    )
    rows = sort_by_source_and_name(assessed_machine_row(item) for item in items)
    return reader.result(
        rows,
        assessment=assessment,
        source_counts=source_counts(rows),
        readiness_counts=readiness_counts(rows),
    )
