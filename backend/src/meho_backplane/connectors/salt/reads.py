# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the six Salt reads: one or more RaaS RPC calls, reduced to flat rows.

Each function takes the connector, the operator, the target and the
validated params, calls :meth:`SaltConnector.rpc` and returns a dict with
one list of rows (plus a few counts), so MEHO can store a long list behind
a result handle.

Field names follow the RaaS 8.18 reference pages and their examples. The
reference shows only some answer keys, so every row reads its fields with
``.get`` and leaves a missing field ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.salt.connector import (
    CMDS_RPC,
    GET_VERSIONS_RPC,
    MASTER_PRESENCE_RPC,
    MINION_CACHE_RPC,
    MINION_KEY_STATE_RPC,
    MINION_PRESENCE_RPC,
    RETURNS_RPC,
    TARGET_GROUP_RPC,
    parse_server_version,
)
from meho_backplane.connectors.salt.returns import RETURN_BYTES_LIMIT, result_row

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.salt.connector import SaltConnector
    from meho_backplane.connectors.salt.session import SaltTargetLike

__all__ = [
    "JOB_FIELDS",
    "JOIN_LIMIT",
    "KEY_PAGE_LIMIT",
    "LIST_PAGE_LIMIT",
    "RETURN_BYTES_LIMIT",
    "about_impl",
    "job_list_impl",
    "job_result_impl",
    "key_list_impl",
    "minion_list_impl",
    "target_list_impl",
]

#: Default page size of the minion and key reads, and of the other list reads.
KEY_PAGE_LIMIT: Final[int] = 100
LIST_PAGE_LIMIT: Final[int] = 50

#: How many masters ``salt.about`` reads.
_MASTER_LIMIT: Final[int] = 100

#: How many presence and cache rows ``salt.minion.list`` reads to join to
#: one page of keys. Beyond it, ``details_complete`` is false.
JOIN_LIMIT: Final[int] = 2000


def _results(ret: Any) -> tuple[list[dict[str, Any]], int | None]:
    """The ``results`` rows and the ``count`` of a paged RaaS answer."""
    if isinstance(ret, list):
        return [row for row in ret if isinstance(row, dict)], None
    if not isinstance(ret, dict):
        return [], None
    rows = ret.get("results")
    count = ret.get("count")
    clean = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return clean, count if isinstance(count, int) else None


def _paging(params: dict[str, Any], default_limit: int) -> dict[str, Any]:
    """``limit`` (the op's default when not given) and ``page`` (left out when not given)."""
    return {"limit": params.get("limit", default_limit), "page": params.get("page")}


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


async def about_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.about`` -- ``api.get_versions`` and ``master.get_master_presence``."""
    del params  # the schema declares no parameters
    versions = await connector.rpc(target, operator, GET_VERSIONS_RPC)
    masters_ret = await connector.rpc(
        target, operator, MASTER_PRESENCE_RPC, {"limit": _MASTER_LIMIT}
    )
    parsed = parse_server_version(versions)
    raas = versions.get("raas") if isinstance(versions, dict) else None
    components = (
        [
            {"name": name, "version": value}
            for name, value in sorted(raas.items())
            if isinstance(value, str)
        ]
        if isinstance(raas, dict)
        else []
    )
    python = versions.get("python") if isinstance(versions, dict) else None
    rows, count = _results(masters_ret)
    masters = [
        {
            "master_id": row.get("master_id"),
            "cluster_id": row.get("cluster_id"),
            "status": row.get("status"),
            "last_seen": row.get("last_seen"),
        }
        for row in rows
    ]
    return {
        "raas_version": parsed.version,
        "raas_version_text": parsed.raw,
        "python_version": python if isinstance(python, str) else None,
        "components": components,
        "master_count": count if count is not None else len(masters),
        "masters": masters,
    }


def _key_kwarg(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "master_id": params.get("master_id"),
        "minion_id": params.get("minion_id"),
        "key_state": params.get("key_state"),
        "sort_by": "minion_id",
        **_paging(params, KEY_PAGE_LIMIT),
    }


def _key_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "minion_id": _first(row, "minion", "minion_id"),
        "master_id": _first(row, "master", "master_id"),
        "key_state": row.get("key_state"),
    }


async def key_list_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.key.list`` -- ``minions.get_minion_key_state``."""
    ret = await connector.rpc(target, operator, MINION_KEY_STATE_RPC, _key_kwarg(params))
    rows, count = _results(ret)
    keys = [_key_row(row) for row in rows]
    return {"count": count if count is not None else len(keys), "keys": keys}


def _presence_map(ret: Any) -> tuple[dict[tuple[Any, Any], dict[str, Any]], bool]:
    rows, count = _results(ret)
    found = {
        (_first(row, "master", "master_id"), _first(row, "minion", "minion_id")): row
        for row in rows
    }
    complete = (count if count is not None else len(rows)) <= len(rows)
    return found, complete


def _grains_map(ret: Any) -> tuple[dict[tuple[Any, Any], dict[str, Any]], bool]:
    found: dict[tuple[Any, Any], dict[str, Any]] = {}
    if isinstance(ret, dict):
        for master, minions in ret.items():
            if not isinstance(minions, dict):
                continue
            for minion, entry in minions.items():
                grains = entry.get("grains") if isinstance(entry, dict) else None
                found[(master, minion)] = grains if isinstance(grains, dict) else {}
    return found, len(found) < JOIN_LIMIT


def _lookup(
    table: dict[tuple[Any, Any], dict[str, Any]], master: Any, minion: Any
) -> dict[str, Any]:
    """The row of (*master*, *minion*); by minion id alone only when the key names no master.

    A minion id can exist under two masters. When the master is known, a
    missing pair stays missing: a row of the other master would report the
    wrong presence or OS.
    """
    if master is not None:
        return table.get((master, minion), {})
    same_minion = [value for (_, name), value in table.items() if name == minion]
    return same_minion[0] if len(same_minion) == 1 else {}


async def minion_list_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.minion.list`` -- one page of keys, joined to presence and grains."""
    ret = await connector.rpc(target, operator, MINION_KEY_STATE_RPC, _key_kwarg(params))
    key_rows, count = _results(ret)
    if not key_rows:
        return {"count": count or 0, "details_complete": True, "minions": []}
    scope = {
        "master_id": params.get("master_id"),
        "minion_id": params.get("minion_id"),
        "limit": JOIN_LIMIT,
    }
    presence, presence_complete = _presence_map(
        await connector.rpc(target, operator, MINION_PRESENCE_RPC, scope)
    )
    grains, grains_complete = _grains_map(
        await connector.rpc(target, operator, MINION_CACHE_RPC, scope)
    )
    minions = []
    for row in key_rows:
        key = _key_row(row)
        seen = _lookup(presence, key["master_id"], key["minion_id"])
        minion_grains = _lookup(grains, key["master_id"], key["minion_id"])
        minions.append(
            {
                **key,
                "presence": seen.get("status"),
                "last_seen": seen.get("timestamp"),
                "os": minion_grains.get("os"),
                "os_release": minion_grains.get("osrelease"),
                "kernel": minion_grains.get("kernel"),
                "salt_version": minion_grains.get("saltversion"),
            }
        )
    return {
        "count": count if count is not None else len(minions),
        "details_complete": presence_complete and grains_complete,
        "minions": minions,
    }


#: The fields of a ``cmd.get_cmds`` row that ``salt.job.list`` returns.
JOB_FIELDS: Final[tuple[str, ...]] = (
    "jid",
    "cmd",
    "fun",
    "state",
    "user",
    "start_time",
    "duration",
    "tgt_name",
    "tgt_uuid",
    "job_name",
    "job_uuid",
    "sched_name",
    "origination",
    "is_highstate",
    "expected",
    "returned",
    "returned_good",
    "returned_failed",
)


async def job_list_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.job.list`` -- ``cmd.get_cmds``, newest first."""
    user = params.get("user")
    kwarg = {
        "state": params.get("state"),
        "fun": params.get("fun"),
        "users": [user] if user else None,
        "jid": params.get("jid"),
        "master_id": params.get("master_id"),
        "sort_by": "start_time",
        "reverse": True,
        **_paging(params, LIST_PAGE_LIMIT),
    }
    ret = await connector.rpc(target, operator, CMDS_RPC, kwarg)
    rows, count = _results(ret)
    jobs = [{field: row.get(field) for field in JOB_FIELDS} for row in rows]
    return {"count": count if count is not None else len(jobs), "jobs": jobs}


async def job_result_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.job.result`` -- ``ret.get_returns`` for one ``jid``, one row per minion."""
    kwarg = {
        "jid": params["jid"],
        "minion_id": params.get("minion_id"),
        "has_errors": params.get("has_errors"),
        "sort_by": "minion_id",
        **_paging(params, LIST_PAGE_LIMIT),
    }
    ret = await connector.rpc(target, operator, RETURNS_RPC, kwarg)
    rows, count = _results(ret)
    results = [result_row(row) for row in rows]
    return {
        "jid": params["jid"],
        "count": count if count is not None else len(results),
        "failed_minions": sum(1 for row in results if row["has_errors"] or row["success"] is False),
        "results": results,
    }


def _target_entries(tgt: Any) -> list[dict[str, Any]]:
    if not isinstance(tgt, dict):
        return []
    entries = []
    for master_id, spec in sorted(tgt.items(), key=lambda item: str(item[0])):
        spec_dict = spec if isinstance(spec, dict) else {}
        entries.append(
            {
                "master_id": master_id,
                "tgt_type": spec_dict.get("tgt_type"),
                "tgt": spec_dict.get("tgt"),
            }
        )
    return entries


async def target_list_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.target.list`` -- ``tgt.get_target_group``, without pillar data."""
    kwarg = {
        "name": params.get("name"),
        "master_id": params.get("master_id"),
        "include_pillar_data": False,
        **_paging(params, LIST_PAGE_LIMIT),
    }
    ret = await connector.rpc(target, operator, TARGET_GROUP_RPC, kwarg)
    rows, count = _results(ret)
    targets = [
        {
            "uuid": row.get("uuid"),
            "name": row.get("name"),
            "desc": row.get("desc"),
            "targets": _target_entries(row.get("tgt")),
        }
        for row in rows
    ]
    return {"count": count if count is not None else len(targets), "targets": targets}
