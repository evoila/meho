# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed (bound-method) read implementations for :class:`NsxConnector`.

The audited NSX read set (#2302) is registered as typed ops
(``source_kind="typed"``) so it dispatches on a fresh boot with **zero
catalog state**. Each function here is the body a thin bound-method shim
on :class:`~meho_backplane.connectors.nsx.connector.NsxConnector`
delegates to; the metadata + registrar live in
:mod:`meho_backplane.connectors.nsx.typed_ops`.

Every read is issued directly on the connector's own authenticated
session via :meth:`HttpConnector._get_json` -- no ``dispatch_child``, no
ingested descriptor. A raw ``401`` from a downstream call propagates as
:class:`httpx.HTTPStatusError` up to the dispatcher's G0.29-T2 (#2067)
recovery arm, which evicts the cached session via the connector's public
:meth:`NsxConnector.invalidate_session` hook and re-dispatches once. The
handlers therefore do **not** wrap calls in the connector's internal
``_get_json_with_session_retry`` helper (that helper serves the
fingerprint/probe path, which the dispatcher does not drive).

The audited set:

* ``nsx.node.status`` -- ``GET /api/v1/node`` (manager identity: version,
  build, node UUID).
* ``nsx.cluster.status`` -- ``GET /api/v1/cluster/status`` (management +
  control cluster health).
* ``nsx.backup.config`` -- ``GET /api/v1/cluster/backups/config``, the
  first-class backup read the adopter flagged (a retention/schedule
  misconfig of the Broadcom KB 442696 class filled a router disk and took
  a lab's DNS down). Secret material (the backup ``passphrase`` and any
  nested SFTP credential) is scrubbed at this boundary -- the default
  connector-boundary redaction policy masks ``password`` / ``secret`` but
  not ``passphrase``, so the scrub happens here (the same
  secret-is-incidental posture the keycloak read ops take).
* ``nsx.backup.status`` -- ``GET /api/v1/cluster/backups/status`` (the
  current backup operation status).
* ``nsx.transport_zone.list`` -- policy-API transport zones under the
  default enforcement point.
* ``nsx.tier1.list`` -- ``GET /policy/api/v1/infra/tier-1s`` (the
  per-tenant east-west routing surface).
* ``nsx.alarm.list`` -- ``GET /api/v1/alarms`` with optional
  ``status`` / ``feature_name`` / ``severity`` filters.

Field names are pinned to the NSX-T Data Center REST API guide
(https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/latest/);
the ``/api/v1/...`` manager and ``/policy/api/v1/...`` policy path
families are stable across the standalone NSX-T 4.x line and the
VCF-9-aligned 9.x line (#1530).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.nsx.session import NsxTargetLike

if TYPE_CHECKING:
    from meho_backplane.connectors.nsx.connector import NsxConnector

__all__ = [
    "REDACTED",
    "nsx_alarm_list_impl",
    "nsx_backup_config_impl",
    "nsx_backup_status_impl",
    "nsx_cluster_status_impl",
    "nsx_node_status_impl",
    "nsx_tier1_list_impl",
    "nsx_transport_zone_list_impl",
]

_log = structlog.get_logger(__name__)

# Spec-relative NSX endpoints. The manager API lives under ``/api/v1``;
# the policy API under ``/policy/api/v1``. Both are reached through the
# connector's pooled per-target client, which carries the JSESSIONID
# cookie + X-XSRF-TOKEN the session establish primed.
_NODE_PATH = "/api/v1/node"
_CLUSTER_STATUS_PATH = "/api/v1/cluster/status"
_BACKUP_CONFIG_PATH = "/api/v1/cluster/backups/config"
_BACKUP_STATUS_PATH = "/api/v1/cluster/backups/status"
_TRANSPORT_ZONES_PATH = (
    "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"
)
_TIER1S_PATH = "/policy/api/v1/infra/tier-1s"
_ALARMS_PATH = "/api/v1/alarms"

#: Sentinel written in place of a scrubbed secret value. A non-empty
#: marker (rather than dropping the key) keeps the shape stable so the
#: agent can see a secret *existed* without learning its value. Same
#: convention :mod:`meho_backplane.connectors.keycloak.redaction` uses.
REDACTED = "***REDACTED***"

#: Key names (matched case-insensitively) whose value -- scalar or
#: subtree -- is secret material scrubbed wherever it appears in the
#: backup configuration. ``passphrase`` is the NSX-specific addition the
#: generic boundary policy does not cover; the rest mirror the well-known
#: credential spellings so a nested SFTP ``password`` under
#: ``remote_file_server.protocol.authentication_scheme`` never leaks.
_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "passphrase",
        "password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "private_key",
    }
)


def _redact_secrets(value: Any) -> Any:
    """Return *value* with secret-keyed material scrubbed, recursively.

    Walks dicts and lists. A key matching :data:`_SECRET_FIELDS`
    (case-insensitively) has its whole value replaced with
    :data:`REDACTED`; every other value is walked so a credential nested
    inside ``remote_file_server`` is still caught. Scalars pass through
    unchanged. The input is not mutated -- a new structure is returned.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in _SECRET_FIELDS
                else _redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


async def nsx_node_status_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.node.status`` -- ``GET /api/v1/node``.

    Returns the NSX Manager node's identity + version: ``node_version``,
    ``kernel_version`` (build), ``node_uuid``, ``hostname``,
    ``external_id``. The same surface :meth:`NsxConnector.fingerprint`
    reads, exposed as an operator-callable typed op for the probe /
    incident-triage question "which NSX build is this and is the manager
    up?".
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _NODE_PATH, operator=operator)


async def nsx_cluster_status_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.cluster.status`` -- ``GET /api/v1/cluster/status``.

    Returns the management-plane health: ``mgmt_cluster_status`` (overall
    management cluster state), ``control_cluster_status``, and per-member
    ``detail``. The read an operator runs when a control-plane outage is
    suspected.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _CLUSTER_STATUS_PATH, operator=operator)


async def nsx_backup_config_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.backup.config`` -- ``GET /api/v1/cluster/backups/config``.

    Returns the automated-backup configuration with the retention-relevant
    fields surfaced and secret material scrubbed. NSX's
    ``BackupConfiguration`` carries ``backup_enabled``, ``backup_schedule``
    (the accumulation rate -- a WeeklyBackupSchedule or an
    IntervalBackupSchedule), ``remote_file_server`` (where backups
    accumulate: ``server`` / ``port`` / ``directory_path`` /
    ``protocol``), ``inventory_summary_interval``,
    ``after_inventory_update_interval``, and a ``passphrase``.

    The disk-fill incident the adopter flagged (Broadcom KB 442696 class)
    is a function of the schedule frequency against the remote server's
    free space, so the returned ``config`` preserves ``backup_schedule``
    and ``remote_file_server`` verbatim (creds masked). ``backup_enabled``
    is hoisted for the at-a-glance answer and ``passphrase_configured``
    reports whether an encryption passphrase is set without returning it.
    """
    del params  # schema declares the param object empty
    raw = await connector._get_json(target, _BACKUP_CONFIG_PATH, operator=operator)
    passphrase = raw.get("passphrase") if isinstance(raw, dict) else None
    _log.info("nsx_backup_config_read", target=target.name)
    return {
        "backup_enabled": raw.get("backup_enabled") if isinstance(raw, dict) else None,
        "passphrase_configured": isinstance(passphrase, str) and bool(passphrase),
        "config": _redact_secrets(raw),
    }


async def nsx_backup_status_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.backup.status`` -- ``GET /api/v1/cluster/backups/status``.

    Returns the current backup operation status
    (``current_backup_operation_status``): whether a backup is running,
    the last operation's success/failure, and its timing. Pairs with
    ``nsx.backup.config`` so the operator sees both "is backup configured
    (and how often)" and "did the last backup actually succeed".
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _BACKUP_STATUS_PATH, operator=operator)


async def nsx_transport_zone_list_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.transport_zone.list`` -- policy-API transport zones.

    ``GET /policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones``
    -- the transport-zone inventory under the default enforcement point.
    Returns ``{"results": [...]}`` where each zone carries ``id``,
    ``display_name``, ``tz_type`` (OVERLAY / VLAN), and
    ``host_switch_name``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _TRANSPORT_ZONES_PATH, operator=operator)


async def nsx_tier1_list_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.tier1.list`` -- ``GET /policy/api/v1/infra/tier-1s``.

    Returns the per-tenant tier-1 gateway inventory (the east-west routing
    surface attached under a tier-0). ``{"results": [...]}`` where each
    tier-1 carries ``id``, ``display_name``, ``tier0_path`` (its parent),
    ``route_advertisement_types``, and ``ha_mode``.
    """
    del params  # schema declares the param object empty
    return await connector._get_json(target, _TIER1S_PATH, operator=operator)


async def nsx_alarm_list_impl(
    connector: NsxConnector,
    operator: Operator,
    target: NsxTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``nsx.alarm.list`` -- ``GET /api/v1/alarms``.

    Returns the NSX system alarms. Optional ``status`` (OPEN / ACKNOWLEDGED
    / SUPPRESSED / RESOLVED), ``feature_name`` (e.g. ``manager_health``),
    and ``severity`` (CRITICAL / HIGH / MEDIUM / LOW) narrow the result to
    the actionable set. ``{"results": [...]}`` where each alarm carries
    ``id``, ``status``, ``severity``, ``feature_name``, ``event_type``,
    ``node_id``, ``last_reported_time``, and ``description``.
    """
    query: dict[str, Any] = {}
    for key in ("status", "feature_name", "severity"):
        value = params.get(key)
        if value:
            query[key] = value
    return await connector._get_json(target, _ALARMS_PATH, operator=operator, params=query or None)
