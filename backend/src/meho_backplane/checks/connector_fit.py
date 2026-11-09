# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check that a Sensor's connector fits its target's product and version.

A Sensor names one connector (``connector_id``). Its target has a detected
product and version (the last probe, else the version an operator set). When
the target's version is outside the version range the connector declares, the
dispatcher does not run the Sensor's operation there, and the Sensor reads
``unknown`` at its next run. This module finds that case before it happens,
so the Sensor list and the create call can warn.

The match is the resolver's own
(:func:`~meho_backplane.connectors.resolver.connector_serves_target`), and the
connector named as the one that fits is the one the resolver picks for the
target (:func:`~meho_backplane.connectors.resolve_connector_or_label`), and
its id is checked with the same match before it is named. No second version
check is written here.

The check runs each time a Sensor is listed, so a new probe that changes the
target's version shows in the next list.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.checks.schemas import SensorConnectorFit, SensorRead
from meho_backplane.connectors import (
    get_connector_v2,
    resolve_connector_or_label,
    resolve_target_version,
)
from meho_backplane.connectors.resolver import connector_serves_target
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations._lookup import parse_connector_id
from meho_backplane.targets.resolver import (
    AmbiguousTargetError,
    TargetNotFoundError,
    resolve_target,
)

__all__ = [
    "connector_fit",
    "connector_fit_for_target",
    "target_name",
    "with_connector_fit",
]


def target_name(target: Any) -> str | None:
    """Return the target name a Sensor stores, or ``None`` when it has none."""
    if not isinstance(target, dict):
        return None
    name = target.get("name")
    return name if isinstance(name, str) and name else None


def connector_fit(connector_id: str, target: Any) -> SensorConnectorFit:
    """Return how *connector_id* fits *target* (a target row).

    No warning when the connector fits, or when MEHO cannot tell: the
    target has no product, the connector is not registered, or the target's
    version is unknown and the product matches.
    """
    product, version, impl_id = parse_connector_id(connector_id)
    target_product = getattr(target, "product", None) or None
    target_version = resolve_target_version(target)
    fit = SensorConnectorFit(target_product=target_product, target_version=target_version)
    connector_class = get_connector_v2(product, version, impl_id)
    if target_product is None or connector_class is None:
        return fit
    if target_product == product:
        if target_version is None or connector_serves_target(product, version, impl_id, target):
            return fit
        problem = (
            f"The target runs {target_product} {target_version}. Connector "
            f"{connector_id} works only with versions "
            f"{connector_class.supported_version_range}."
        )
    else:
        problem = (
            f"The target is a {target_product} target. Connector {connector_id} is for {product}."
        )
    matching = _matching_connector_id(target, connector_class)
    if matching is not None:
        advice = f" Connector {matching} fits this target."
    else:
        advice = " No connector in MEHO fits this target."
    return fit.model_copy(
        update={
            "warning": problem + advice + " The Sensor may read unknown until this is fixed.",
            "matching_connector_id": matching,
        }
    )


def _matching_connector_id(target: Any, own_class: type[Any]) -> str | None:
    """The id of the connector the resolver picks for *target*, if not *own_class*.

    The resolver returns a class, not the registration it picked. A class
    can be registered twice: once under its version, and once as a fallback
    that accepts every version. The id built from the class names the
    versioned registration, which may not fit the target the fallback was
    picked for. So the id is checked the way the create check checks it,
    and ``None`` comes back when it does not fit.
    """
    resolved, _label, _message = resolve_connector_or_label(target)
    if resolved is None or resolved is own_class:
        return None
    if resolved.impl_id and resolved.version:
        connector_id = f"{resolved.impl_id}-{resolved.version}"
    else:
        connector_id = str(resolved.product)
    if not connector_serves_target(*parse_connector_id(connector_id), target):
        return None
    return connector_id


async def _find_target(session: AsyncSession, tenant_id: uuid.UUID, name: str) -> Any:
    """Look a target up by name or alias; ``None`` when there is no single match.

    :func:`resolve_target` binds the target into the log context for the
    audit row. A Sensor read is not about one target, so the binding is
    undone when the lookup returns.
    """
    with structlog.contextvars.bound_contextvars(target_id=None, target_name=None):
        try:
            return await resolve_target(session, tenant_id, name)
        except (TargetNotFoundError, AmbiguousTargetError):
            return None


async def connector_fit_for_target(
    tenant_id: uuid.UUID, connector_id: str, target: Any
) -> SensorConnectorFit | None:
    """Return the fit for one stored Sensor *target*, or ``None`` without one."""
    name = target_name(target)
    if name is None:
        return None
    async with get_sessionmaker()() as session:
        row = await _find_target(session, tenant_id, name)
    return None if row is None else connector_fit(connector_id, row)


async def with_connector_fit(
    tenant_id: uuid.UUID, sensors: Sequence[SensorRead]
) -> list[SensorRead]:
    """Return *sensors* with ``connector_fit`` filled in.

    Each target name is looked up once in *tenant_id*. A Sensor without a
    target, or whose target is not found, keeps ``connector_fit=None``.
    """
    names = {name for s in sensors if (name := target_name(s.target)) is not None}
    if not names:
        return list(sensors)
    async with get_sessionmaker()() as session:
        rows = {name: await _find_target(session, tenant_id, name) for name in sorted(names)}
    out: list[SensorRead] = []
    for sensor in sensors:
        name = target_name(sensor.target)
        row = rows.get(name) if name is not None else None
        if row is None:
            out.append(sensor)
            continue
        out.append(
            sensor.model_copy(update={"connector_fit": connector_fit(sensor.connector_id, row)})
        )
    return out
