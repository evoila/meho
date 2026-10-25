# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What ``salt.job.result`` shows of one job return row.

A job return can hold a password under no name a rule could match: a
``cmd.run`` output, a pillar value, a positional argument, the command of
a ``cmd`` state. So a row shows only what can be checked: the arguments of
a short list of functions that take names and keys, a number or boolean
return, and the checked fields of each state of a state run. Everything
else is ``null`` with a ``*_withheld`` flag. :mod:`.trace` runs the stored
answer of the same RPC through :func:`result_row` too.
"""

from __future__ import annotations

import re
from typing import Any, Final

from meho_backplane.connectors._shared.api_file_values import REDACTED, Scrubber, dump_json

__all__ = ["RETURN_BYTES_LIMIT", "result_row"]

#: Largest job return per minion (JSON bytes) that ``salt.job.result``
#: returns whole. A larger return comes back as ``null`` with its size.
RETURN_BYTES_LIMIT: Final[int] = 8192

#: Largest function argument list (JSON bytes) a row keeps.
_FUN_ARGS_BYTES_LIMIT: Final[int] = 2048

#: How many failed states one result row names.
_FAILED_STATES_LIMIT: Final[int] = 20


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


#: Keyword arguments whose value is data, not a name: the ``pillar`` override
#: of a state run and the ``default`` of ``pillar.get`` and ``grains.get``,
#: which can be a password. ``_NAME_ARG_COUNT`` says how many leading
#: positional arguments of those two functions are names; the ``default``
#: that follows is a value. Every other function of the list takes only names.
_VALUE_KWARGS: Final[frozenset[str]] = frozenset({"pillar", "default"})
_NAME_ARG_COUNT: Final[dict[str, int]] = {"grains.get": 1, "pillar.get": 1}


def _hide_value_args(node: Any) -> Any:
    """Replace the value of a ``pillar`` or ``default`` argument (dict key or ``key=...`` text)."""
    if isinstance(node, dict):
        return {
            key: REDACTED if key in _VALUE_KWARGS else _hide_value_args(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_hide_value_args(item) for item in node]
    if isinstance(node, str):
        key, sep, _ = node.partition("=")
        if sep and key in _VALUE_KWARGS:
            return f"{key}={REDACTED}"
    return node


def _hide_value_positions(fun: Any, args: Any) -> Any:
    """Replace the positional arguments of *fun* that are values, not names."""
    count = _NAME_ARG_COUNT.get(fun) if isinstance(fun, str) else None
    if count is None or not isinstance(args, list):
        return args
    positional = 0
    kept: list[Any] = []
    for item in args:
        if isinstance(item, dict) and item.get("__kwarg__"):
            kept.append(item)
            continue
        kept.append(item if positional < count else REDACTED)
        positional += 1
    return kept


#: State modules whose ``name`` is a path, a package, a service, a user, a
#: group or a kernel setting. The ``name`` of a ``cmd`` state is the
#: command, which can carry a password; the ``name`` of a ``git`` or
#: ``pip`` state can be a URL with one. Those names stay out, as do the
#: state id and the comment (free text that can quote the command).
_NAME_STATE_MODULES: Final[frozenset[str]] = frozenset(
    {"file", "group", "pkg", "service", "sysctl", "user"}
)

_SLS_RE: Final[re.Pattern[str]] = re.compile(r"^[\w./-]+$")
_TIME_RE: Final[re.Pattern[str]] = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?$")


def _state_run(value: Any) -> dict[str, Any] | None:
    """The states of a state run return (every value has a ``result``), else ``None``."""
    if not isinstance(value, dict) or not value:
        return None
    if not all(isinstance(item, dict) and "result" in item for item in value.values()):
        return None
    return value


def _state_kind(key: str) -> str | None:
    """``module.function`` of a state from its return key ``module_|-id_|-name_|-function``."""
    parts = key.split("_|-")
    if len(parts) != 4 or not (parts[0].isidentifier() and parts[3].isidentifier()):
        return None
    return f"{parts[0]}.{parts[3]}"


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def _state_entry(key: str, item: dict[str, Any]) -> dict[str, Any]:
    """The checked fields of one state: its kind, outcome and timing, no free text.

    The ``changes`` (a file diff, a command's output), the ``comment`` and
    the state id are free text that no rule can check. The ``name`` is
    kept only for the state modules whose name is a path or an object name.
    """
    kind = _state_kind(key)
    named = kind is not None and kind.partition(".")[0] in _NAME_STATE_MODULES
    result = item.get("result")
    return {
        "state": kind,
        "name": item.get("name") if named and isinstance(item.get("name"), str) else None,
        "sls": _text(item.get("__sls__"), _SLS_RE),
        "result": result if isinstance(result, bool) else None,
        "changed": bool(item.get("changes")),
        "run_num": _number(item.get("__run_num__")),
        "start_time": _text(item.get("start_time"), _TIME_RE),
        "duration": _number(item.get("duration")),
    }


def _checked_return(value: Any, states: dict[str, Any] | None) -> tuple[Any, bool]:
    """The return the row may show, and whether its content could be checked.

    A state run is shown as a list with the checked fields of each state.
    A number, a boolean or ``None`` holds no secret. Everything else (a
    text such as ``file.read`` or ``cmd.run`` returns, a list, any other
    dict) can hold a secret under no name, so it is not shown.
    """
    if states is not None:
        return [_state_entry(key, item) for key, item in states.items()], True
    if value is None or isinstance(value, (bool, int, float)):
        return value, True
    return None, False


def _state_summary(states: dict[str, Any] | None) -> dict[str, Any]:
    """Count the states of a state run return and list the failed ones."""
    if states is None:
        return {"states_total": None, "states_failed": None, "failed_states": []}
    failed = [(key, item) for key, item in states.items() if item.get("result") is False]
    return {
        "states_total": len(states),
        "states_failed": len(failed),
        "failed_states": [_state_entry(key, item) for key, item in failed[:_FAILED_STATES_LIMIT]],
    }


def result_row(row: dict[str, Any]) -> dict[str, Any]:
    full_ret = row.get("full_ret")
    full: dict[str, Any] = full_ret if isinstance(full_ret, dict) else {}
    fun = row.get("fun") or full.get("fun")
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
    fun_args = _hide_value_args(_hide_value_positions(fun, Scrubber().tree(raw_args, {})))
    fun_args_kept = fun_args if _json_size(fun_args) <= _FUN_ARGS_BYTES_LIMIT else None
    return {
        "minion_id": row.get("minion_id") or full.get("id"),
        "master_id": row.get("master") or full.get("master_id"),
        "jid": row.get("jid") or full.get("jid"),
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
