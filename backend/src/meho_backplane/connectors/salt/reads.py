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

from meho_backplane.connectors._shared.api_file_values import REDACTED, Scrubber, dump_json
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

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.salt.connector import SaltConnector
    from meho_backplane.connectors.salt.session import SaltTargetLike

__all__ = [
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

#: Largest job return per minion (JSON bytes) that ``salt.job.result``
#: returns whole. A larger return comes back as ``null`` with its size.
RETURN_BYTES_LIMIT: Final[int] = 8192

#: Largest function argument list (JSON bytes) a row keeps.
_FUN_ARGS_BYTES_LIMIT: Final[int] = 2048

#: How many failed states one result row names, and how long a comment is.
_FAILED_STATES_LIMIT: Final[int] = 20
_COMMENT_LIMIT: Final[int] = 500


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


_JOB_FIELDS: Final[tuple[str, ...]] = (
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
    jobs = [{field: row.get(field) for field in _JOB_FIELDS} for row in rows]
    return {"count": count if count is not None else len(jobs), "jobs": jobs}


def _json_size(value: Any) -> int:
    return len(dump_json(value).encode("utf-8"))


#: Salt functions outside ``pillar.*`` that return pillar values.
_PILLAR_CONFIG_FUNS: Final[frozenset[str]] = frozenset(
    {"config.get", "config.option", "config.items"}
)


def _reads_pillar(fun: Any) -> bool:
    """True when the Salt function returns pillar data, which holds secrets."""
    return isinstance(fun, str) and (fun.startswith("pillar.") or fun in _PILLAR_CONFIG_FUNS)


#: Salt functions whose arguments are only names and keys: state names,
#: package and service names, grain and pillar keys. A positional argument
#: has no name the scrubber could match (``user.setpassword <user>
#: <password>``), and one module can mix name arguments with value
#: arguments (``grains.setval <key> <value>``, ``pkg.install sources=<url>``),
#: so the list names functions, not modules. The arguments of every other
#: function stay out.
_SAFE_ARG_FUNS: Final[frozenset[str]] = frozenset(
    {
        "grains.get",
        "grains.has_value",
        "grains.item",
        "grains.items",
        "grains.ls",
        "pillar.get",
        "pillar.item",
        "pillar.items",
        "pillar.keys",
        "pkg.latest_version",
        "pkg.list_pkgs",
        "pkg.list_upgrades",
        "pkg.purge",
        "pkg.refresh_db",
        "pkg.remove",
        "pkg.upgrade",
        "pkg.version",
        "service.available",
        "service.disable",
        "service.disabled",
        "service.enable",
        "service.enabled",
        "service.get_all",
        "service.reload",
        "service.restart",
        "service.running",
        "service.start",
        "service.status",
        "service.stop",
        "state.apply",
        "state.highstate",
        "state.show_highstate",
        "state.show_sls",
        "state.show_top",
        "state.sls",
        "state.test",
        "test.ping",
        "test.version",
        "test.versions_report",
    }
)


def _keeps_args(fun: Any) -> bool:
    """True when the arguments of the Salt function *fun* can be shown."""
    return isinstance(fun, str) and fun in _SAFE_ARG_FUNS


def _hide_pillar_args(node: Any) -> Any:
    """Replace the value of every ``pillar`` argument (a dict key or ``pillar=...`` text)."""
    if isinstance(node, dict):
        return {
            key: REDACTED if key == "pillar" else _hide_pillar_args(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_hide_pillar_args(item) for item in node]
    if isinstance(node, str) and node.startswith("pillar="):
        return f"pillar={REDACTED}"
    return node


#: The fields of one state in a state run return that the row keeps. The
#: ``changes`` of a state (a file diff, a command's output) are free text
#: that no rule can check, so the row keeps only whether there were any.
_STATE_FIELDS: Final[tuple[str, ...]] = (
    "result",
    "name",
    "comment",
    "__sls__",
    "__id__",
    "__run_num__",
    "start_time",
    "duration",
)


def _state_run(value: Any) -> dict[str, Any] | None:
    """The states of a state run return (every value has a ``result``), else ``None``."""
    if not isinstance(value, dict) or not value:
        return None
    if not all(isinstance(item, dict) and "result" in item for item in value.values()):
        return None
    return value


def _state_entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = {field: item.get(field) for field in _STATE_FIELDS}
    comment = entry["comment"]
    if isinstance(comment, str) and len(comment) > _COMMENT_LIMIT:
        entry["comment"] = comment[:_COMMENT_LIMIT] + "..."
    entry["changed"] = bool(item.get("changes"))
    return entry


def _checked_return(value: Any, states: dict[str, Any] | None) -> tuple[Any, bool]:
    """The return the row may show, and whether its content could be checked.

    A state run is shown with the known fields of each state. A number, a
    boolean or ``None`` holds no secret. Everything else (a text such as
    ``file.read`` or ``cmd.run`` returns, a list, any other dict) can hold
    a secret under no name, so it is not shown.
    """
    if states is not None:
        return {key: _state_entry(item) for key, item in states.items()}, True
    if value is None or isinstance(value, (bool, int, float)):
        return value, True
    return None, False


def _state_summary(states: dict[str, Any] | None) -> dict[str, Any]:
    """Count the states of a state run return and name the failed ones."""
    if states is None:
        return {"states_total": None, "states_failed": None, "failed_states": []}
    failed = [(key, item) for key, item in states.items() if item.get("result") is False]
    failed_states = []
    for key, item in failed[:_FAILED_STATES_LIMIT]:
        entry = _state_entry(item)
        failed_states.append({"state": key, "name": entry["name"], "comment": entry["comment"]})
    return {
        "states_total": len(states),
        "states_failed": len(failed),
        "failed_states": failed_states,
    }


def _result_row(row: dict[str, Any]) -> dict[str, Any]:
    full_ret = row.get("full_ret")
    full: dict[str, Any] = full_ret if isinstance(full_ret, dict) else {}
    fun = _first(row, "fun") or full.get("fun")
    pillar = _reads_pillar(fun)
    raw_return = row["return"] if "return" in row else full.get("return")
    scrubbed = Scrubber().tree(raw_return, {})
    size = _json_size(scrubbed)
    states = None if pillar else _state_run(scrubbed)
    shown, checked = _checked_return(scrubbed, states)
    withheld = pillar or not checked
    truncated = size > RETURN_BYTES_LIMIT
    args_withheld = not _keeps_args(fun)
    raw_args = None
    if not args_withheld:
        raw_args = row.get("fun_args")
        if raw_args is None:
            raw_args = full.get("fun_args")
    fun_args = _hide_pillar_args(Scrubber().tree(raw_args, {}))
    fun_args_kept = fun_args if _json_size(fun_args) <= _FUN_ARGS_BYTES_LIMIT else None
    return {
        "minion_id": _first(row, "minion_id") or full.get("id"),
        "master_id": _first(row, "master") or full.get("master_id"),
        "jid": _first(row, "jid") or full.get("jid"),
        "fun": fun,
        "fun_args": fun_args_kept,
        "fun_args_withheld": args_withheld,
        "success": full.get("success"),
        "retcode": full.get("retcode"),
        "has_errors": row.get("has_errors"),
        "alter_time": row.get("alter_time"),
        "return": shown if not withheld and not truncated else None,
        "return_bytes": size,
        "return_truncated": truncated,
        "return_withheld": withheld,
        **_state_summary(states),
    }


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
    results = [_result_row(row) for row in rows]
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
