# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The dashboard file: one Dashboard and its Sensors, ready for another site.

``meho dashboard export <name>`` writes a :class:`DashboardFile`.
``meho dashboard import <file>`` reads it back, at the same site or at
another one. The file holds the settings that make a check: the operation,
its params, the rule, the cadence and the severity. It holds no ids, no
state and no mail address, because those belong to one site.

A Sensor's target is a placeholder: the target name at the site that
exported the file. The import maps every placeholder to a target of its
own site (``target_map``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.dashboard_schemas import (
    _DESCRIPTION_MAX_LENGTH,
    _INVESTIGATOR_PROMPT_MAX_LENGTH,
    _MAX_MEMBERS,
    NotifyMinState,
)
from meho_backplane.checks.dashboard_schemas import _NAME_MAX_LENGTH as _DASHBOARD_NAME_MAX
from meho_backplane.checks.schemas import (
    _CONNECTOR_ID_MAX_LENGTH,
    _CRON_EXPR_MAX_LENGTH,
    _INTERVAL_SECONDS_MAX,
    _INTERVAL_SECONDS_MIN,
    _NAME_MAX_LENGTH,
    _OP_ID_MAX_LENGTH,
    _RETRY_BACKOFF_SECONDS_MAX,
    _RETRY_BACKOFF_SECONDS_MIN,
    _RETRY_TIMES_MAX,
    _TIMEZONE_MAX_LENGTH,
    SensorCreate,
)
from meho_backplane.db.models import SensorCadenceKind, SensorSeverity

__all__ = [
    "DASHBOARD_FILE_FORMAT",
    "DashboardFile",
    "DashboardFileSensor",
    "DashboardFileSettings",
    "DashboardImportRequest",
    "DashboardImportResult",
    "ImportAction",
    "ImportedSensor",
]

#: The format tag of the file. A later change to the file shape gets a new tag.
DASHBOARD_FILE_FORMAT: Final = "meho-dashboard/1"

#: Max length of a target placeholder or a target name in the map.
_TARGET_NAME_MAX_LENGTH = 256

ImportAction = Literal["create", "update", "unchanged"]


class DashboardFileSettings(BaseModel):
    """The Dashboard part of the file. The mail address stays at each site."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=_DASHBOARD_NAME_MAX)
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX_LENGTH)
    notify_min_state: NotifyMinState = "critical"
    investigator_prompt: str | None = Field(
        default=None, max_length=_INVESTIGATOR_PROMPT_MAX_LENGTH
    )


class DashboardFileSensor(BaseModel):
    """One Sensor in the file: the fields of a create, with a target placeholder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=_NAME_MAX_LENGTH)]
    connector_id: Annotated[str, Field(min_length=1, max_length=_CONNECTOR_ID_MAX_LENGTH)]
    op_id: Annotated[str, Field(min_length=1, max_length=_OP_ID_MAX_LENGTH)]
    #: The target placeholder, or ``None`` for an operation without a target.
    target: Annotated[str, Field(min_length=1, max_length=_TARGET_NAME_MAX_LENGTH)] | None = None
    params: dict[str, object] = Field(default_factory=dict)
    assertion: AssertionSpec
    cadence_kind: SensorCadenceKind
    interval_seconds: Annotated[
        int | None, Field(ge=_INTERVAL_SECONDS_MIN, le=_INTERVAL_SECONDS_MAX)
    ] = None
    cron_expr: Annotated[str | None, Field(max_length=_CRON_EXPR_MAX_LENGTH)] = None
    timezone: Annotated[str, Field(max_length=_TIMEZONE_MAX_LENGTH)] = "UTC"
    severity: SensorSeverity = SensorSeverity.CRITICAL
    for_seconds: Annotated[int, Field(ge=0)] = 0
    retry_times: Annotated[int, Field(ge=0, le=_RETRY_TIMES_MAX)] = 0
    retry_backoff_seconds: Annotated[
        int, Field(ge=_RETRY_BACKOFF_SECONDS_MIN, le=_RETRY_BACKOFF_SECONDS_MAX)
    ] = 15

    @model_validator(mode="after")
    def _check_like_a_create(self) -> DashboardFileSensor:
        # The cadence and rule checks of a create run on the file too.
        self.to_create(target_name=None)
        return self

    def to_create(self, *, target_name: str | None) -> SensorCreate:
        """Return the create body of this Sensor, with the real target name."""
        return SensorCreate(
            name=self.name,
            connector_id=self.connector_id,
            op_id=self.op_id,
            target=None if target_name is None else {"name": target_name},
            params=self.params,
            assertion=self.assertion,
            cadence_kind=self.cadence_kind,
            interval_seconds=self.interval_seconds,
            cron_expr=self.cron_expr,
            timezone=self.timezone,
            severity=self.severity,
            for_seconds=self.for_seconds,
            retry_times=self.retry_times,
            retry_backoff_seconds=self.retry_backoff_seconds,
        )


class DashboardFile(BaseModel):
    """One Dashboard and its member Sensors, as ``meho dashboard export`` writes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["meho-dashboard/1"] = DASHBOARD_FILE_FORMAT
    dashboard: DashboardFileSettings
    sensors: list[DashboardFileSensor] = Field(default_factory=list, max_length=_MAX_MEMBERS)

    @model_validator(mode="after")
    def _unique_sensor_names(self) -> DashboardFile:
        names = [s.name for s in self.sensors]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValueError(f"a sensor name is in the file more than once: {', '.join(twice)}")
        return self

    def placeholders(self) -> list[str]:
        """Every target placeholder the Sensors use, sorted."""
        return sorted({s.target for s in self.sensors if s.target is not None})


class DashboardImportRequest(BaseModel):
    """Request body for ``POST /api/v1/checks/dashboard-files/import``.

    * ``target_map`` -- placeholder -> target name at this site. Every
      placeholder in the file needs an entry.
    * ``dry_run`` -- say what would change, and change nothing.
    * ``force`` -- import a Sensor even when its connector does not fit the
      mapped target's product and version (as ``force`` on a Sensor create).
    * ``tenant_id`` -- a platform admin may import into another tenant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: DashboardFile
    target_map: dict[
        Annotated[str, Field(min_length=1, max_length=_TARGET_NAME_MAX_LENGTH)],
        Annotated[str, Field(min_length=1, max_length=_TARGET_NAME_MAX_LENGTH)],
    ] = Field(default_factory=dict, max_length=_MAX_MEMBERS)
    dry_run: bool = False
    force: bool = False
    tenant_id: uuid.UUID | None = None


class ImportedSensor(BaseModel):
    """What the import did, or would do, to one Sensor."""

    model_config = ConfigDict(frozen=True)

    name: str
    action: ImportAction
    #: The fields that change on an update; empty for create and unchanged.
    changed_fields: list[str] = Field(default_factory=list)


class DashboardImportResult(BaseModel):
    """The answer of an import. With ``dry_run`` nothing was written."""

    model_config = ConfigDict(frozen=True)

    dry_run: bool
    #: ``False`` when the tenant already matched the file: nothing changed.
    changed: bool
    #: ``None`` only on a dry run that would create the Dashboard.
    dashboard_id: uuid.UUID | None
    dashboard_name: str
    dashboard_action: ImportAction
    dashboard_changed_fields: list[str] = Field(default_factory=list)
    sensors: list[ImportedSensor]
    #: Sensor names that join or leave the Dashboard. A Sensor that leaves
    #: is not deleted.
    members_added: list[str] = Field(default_factory=list)
    members_removed: list[str] = Field(default_factory=list)
