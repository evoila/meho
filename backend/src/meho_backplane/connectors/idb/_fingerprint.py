# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fingerprint of the ``idb`` connectors: which product answers, and its version.

1. ``GET /SAAS/API/1.0/REST/system/health`` without a login. An answer with
   ``AllOk`` is Workspace ONE Access. Its version comes from a version field
   in that answer, when there is one.
2. Otherwise ``GET /acs/t/{tenant}/.well-known/openid-configuration`` without
   a login. An answer with ``issuer`` is Identity Broker. Its version comes
   from ``GET /acs/t/{tenant}/properties`` (9.1 and newer; needs the login).

When no version is found, ``version`` stays ``None`` and the operator sets
the version on the target.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.idb import _paths as paths
from meho_backplane.connectors.idb.session import resolve_tenant
from meho_backplane.connectors.schemas import FingerprintResult

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.idb.connector import IdbConnectorBase
    from meho_backplane.connectors.idb.session import IdbTargetLike

__all__ = ["fingerprint_target", "health_version", "parse_version"]

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"\d+\.\d+(?:\.\d+)*")


def parse_version(raw: object) -> str | None:
    """The leading ``X.Y[.Z...]`` part of a version text, or ``None``."""
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.search(raw)
    return match.group(0) if match else None


def health_version(health: dict[str, Any]) -> str | None:
    """The version in a Workspace ONE Access health answer, or ``None``.

    The 3.3 health docs do not list a version field, so any field whose name
    holds ``version`` is read (for example ``BuildVersion``).
    """
    for key, value in health.items():
        if "version" in str(key).lower():
            found = parse_version(value)
            if found:
                return found
    return None


async def _get_open(conn: IdbConnectorBase, target: IdbTargetLike, path: str) -> Any:
    """``GET`` without a login; the JSON answer of a 200, else ``None``."""
    client = await conn._http_client(target)
    response = await client.get(
        path,
        headers={"Accept": "application/json"},
        extensions=conn._request_extensions(target),
    )
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _result(method: str, probed_at: datetime, **fields: Any) -> FingerprintResult:
    return FingerprintResult(
        vendor="vmware", product="idb", probed_at=probed_at, probe_method=method, **fields
    )


async def _broker_version(
    conn: IdbConnectorBase, target: IdbTargetLike, operator: Operator | None, tenant: str
) -> tuple[str | None, str | None]:
    """The Identity Broker version from ``properties``, and the error name when it failed."""
    try:
        props = await conn._request_json(
            target,
            "GET",
            paths.fill(paths.IDB_PROPERTIES_PATH, {"tenant": tenant}),
            operator=operator if operator is not None else synthesise_system_operator(),
        )
    except Exception as exc:  # the version is optional; the target is reachable
        return None, type(exc).__name__
    return parse_version(props.get("version")), None


async def fingerprint_target(
    conn: IdbConnectorBase, target: IdbTargetLike, operator: Operator | None
) -> FingerprintResult:
    """Run the two checks of the module docstring and return the fingerprint."""
    probed_at = datetime.now(UTC)
    method = f"GET {paths.WSA_HEALTH_PATH}"
    try:
        health = await _get_open(conn, target, paths.WSA_HEALTH_PATH)
        if isinstance(health, dict) and "AllOk" in health:
            return _result(
                method,
                probed_at,
                version=health_version(health),
                reachable=True,
                extras={"product_line": "workspace-one-access"},
            )
        tenant = resolve_tenant(target)
        method = f"GET {paths.IDB_OIDC_DISCOVERY_PATH}"
        discovery = await _get_open(
            conn, target, paths.fill(paths.IDB_OIDC_DISCOVERY_PATH, {"tenant": tenant})
        )
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        return _result(
            method, probed_at, reachable=False, extras={"error": f"{type(exc).__name__}: {exc}"}
        )
    if not (isinstance(discovery, dict) and "issuer" in discovery):
        return _result(
            method,
            probed_at,
            reachable=False,
            extras={"error": "neither Workspace ONE Access nor Identity Broker answered"},
        )
    version, error = await _broker_version(conn, target, operator, tenant)
    extras: dict[str, Any] = {"product_line": "identity-broker", "tenant": tenant}
    if error is not None:
        extras["version_error"] = error
    return _result(method, probed_at, version=version, reachable=True, extras=extras)
