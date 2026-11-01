# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Alarm acknowledge and reset.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.alarm_state import (
    ALARMS_GROUP_KEY,
)
from meho_backplane.connectors.vmware_rest.composites import _alarm
from meho_backplane.connectors.vmware_rest.composites._alarm import (
    alarm_acknowledge_composite,
    alarm_acknowledge_preview,
    alarm_reset_composite,
    alarm_reset_preview,
)
from meho_backplane.connectors.vmware_rest.composites._alarm_schemas import (
    ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS,
    ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA,
    ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA,
    ALARM_RESET_LLM_INSTRUCTIONS,
    ALARM_RESET_PARAMETER_SCHEMA,
    ALARM_RESET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.alarm.acknowledge",
        handler=alarm_acknowledge_composite,
        summary="Acknowledge one triggered vCenter alarm on one object, so its actions stop.",
        description=(
            "Acknowledges one triggered alarm on one object (AlarmManager.AcknowledgeAlarm). "
            "The alarm stays red or yellow, but its actions do not fire again until it was "
            "green once. Reads the alarm before and after. An acknowledged alarm gives "
            "unchanged."
        ),
        parameter_schema=ALARM_ACKNOWLEDGE_PARAMETER_SCHEMA,
        response_schema=ALARM_ACKNOWLEDGE_RESPONSE_SCHEMA,
        group_key=ALARMS_GROUP_KEY,
        tags=["composite", "write", "alarms", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=ALARM_ACKNOWLEDGE_LLM_INSTRUCTIONS,
        preview=alarm_acknowledge_preview,
        governed_subops=_alarm._VIM_SUB_OPS_ALARM_ACKNOWLEDGE,
    ),
    CompositeSpec(
        op_id="vmware.composite.alarm.reset",
        handler=alarm_reset_composite,
        summary="Reset triggered vCenter alarms to green after their cause is fixed.",
        description=(
            "Resets to green every triggered alarm in the vCenter with the given status, "
            "object kind and trigger kind (ClearTriggeredAlarms); vCenter cannot reset one "
            "alarm alone. This hides a problem until the alarm fires again: fix the cause "
            "first. The answer lists what was cleared."
        ),
        parameter_schema=ALARM_RESET_PARAMETER_SCHEMA,
        response_schema=ALARM_RESET_RESPONSE_SCHEMA,
        group_key=ALARMS_GROUP_KEY,
        tags=["composite", "write", "alarms", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=ALARM_RESET_LLM_INSTRUCTIONS,
        preview=alarm_reset_preview,
        governed_subops=_alarm._VIM_SUB_OPS_ALARM_RESET,
    ),
)
