# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the Salt job changes: run a state, run a command, stop a job.

Run a state or a command
------------------------

Both are ``destructive``: a state or a command can change anything on many
machines. The flow:

1. **Preview.** The preview builder resolves the target (a saved target
   or a target expression) to the minions it matches today, with
   ``tgt.get_target_group_minions``. The approval card names the Salt
   function, its arguments (the command text in full), the target as the
   caller gave it, every matched minion, and a hash of the minion list.
   It refuses a target that matches no minion or too many.
2. **Approved run.** The body resolves the target again. When the minion
   list, the function or the arguments differ from the card, it stops with
   ``precondition_failed`` and starts nothing.
3. **Start.** ``cmd.route_cmd`` runs the function with a ``list`` target
   that names exactly the approved minions, per master. So the run never
   reaches a minion the approver did not see, even when the target
   expression would match more minions by now.

The start returns as soon as RaaS accepted the job, with its ``jid``. The
caller follows the job with ``salt.job.result``.

Before a start, the body looks for a running job of the same function on
the same minions (``cmd.get_cmds``). When one runs, it returns that job's
``jid`` and starts nothing. This is a help, not a lock.

A command whose text looks like it holds a secret (a ``password=...``
argument, a password in a common command-line form, or text the shared
redaction engine would change) is refused, so the text can be shown in full
on the card, in the audit row and in the trace.

The command goes to ``cmd.run`` as the keyword ``cmd``, and the state names
to ``state.apply`` as the keyword ``mods``. A minion reads a plain argument
of the form ``name=value`` as a keyword, so a command such as
``LANG=C locale`` would otherwise not reach ``cmd.run`` as its command.

Stop a job
----------

``salt.job.cancel`` reads the job with ``cmd.get_cmds`` and, when it has not
ended, calls ``cmd.stop_job`` (the vendor's soft stop).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.connectors.salt._op_common import (
    JOB_STATUS_BY_STATE,
    RUNNING_JOB_STATES,
)
from meho_backplane.connectors.salt.connector import (
    CMDS_RPC,
    ROUTE_CMD_RPC,
    STOP_JOB_RPC,
    TARGET_MINIONS_RPC,
    SaltChangeRefusedError,
)
from meho_backplane.connectors.salt.reads import _results
from meho_backplane.operations._preview import (
    PreviewContext,
    approved_preview,
    register_preview_builder,
)
from meho_backplane.redaction import get_default_policy, redact

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.salt.connector import SaltConnector
    from meho_backplane.connectors.salt.session import SaltTargetLike

__all__ = [
    "MAX_RUN_MINIONS",
    "RUN_COMMAND_OP_ID",
    "RUN_STATE_OP_ID",
    "STATUS_READ",
    "TGT_TYPES",
    "command_secret_reason",
    "job_cancel_impl",
    "minions_sha256",
    "run_command_impl",
    "run_state_impl",
]

RUN_STATE_OP_ID: Final[str] = "salt.job.run_state"
RUN_COMMAND_OP_ID: Final[str] = "salt.job.run_command"

#: The read that follows a started job.
STATUS_READ: Final[str] = "salt.job.result"

#: Most minions one run may reach. A bigger target is refused; narrow it.
MAX_RUN_MINIONS: Final[int] = 500

#: Target types, as the ``tgt`` page of the RaaS reference lists them.
TGT_TYPES: Final[tuple[str, ...]] = (
    "glob",
    "list",
    "grain",
    "grain_pcre",
    "pillar",
    "pillar_pcre",
    "ipcidr",
    "pcre",
    "node",
    "compound",
)

#: A minion or master id MEHO can put into a ``list`` target: no comma, no
#: space and no glob character, so it matches one minion only.
_PLAIN_ID: Final[re.Pattern[str]] = re.compile(r"[^\s,*?\[\]]+")

#: ``name=value`` or ``name: value`` in a command, and ``--name value``.
_ASSIGNED_NAME: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s;&|(\"'])-{0,2}([A-Za-z_][\w.-]*)\s*[=:]"
)
_FLAG_NAME: Final[re.Pattern[str]] = re.compile(r"(?:^|\s)-{1,2}([A-Za-z][\w-]*)\s+\S")

#: Common command-line forms that carry a password without a secret-shaped name.
_SECRET_FORMS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # -u user:password, --user user:password
        r"(?:^|\s)(?:-u|--user)(?:\s+|=)?[^\s:]*:\S",
        # scheme://user:password@host
        r"[a-z][a-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@",
        r"\bsshpass\b[^|;&]*\s-p",
        # mysql -p<value>; a bare -p asks for the password
        r"\b(?:mysql\w*|mariadb\w*)\b[^|;&]*\s-p\S",
        r"\|\s*(?:sudo\s+(?:-\S+\s+)*)?(?:passwd|chpasswd)\b",
        r"\b(?:passwd|chpasswd)\b[^|;&]*<<<",
        r"\bhtpasswd\b[^|;&]*\s-[a-z]*b",
        # net user <name> <password>; * asks for it, /option is no password
        r"\bnet(?:\.exe)?\s+user\s+\S+\s+[^\s/*]",
    )
)

_RUN_IRREVERSIBILITY: Final[str] = (
    "A Salt state or command can change anything on each minion, and Salt cannot undo "
    "it. Only a later state or command can change it back."
)


def _secret_name(name: str) -> bool:
    low = name.lower().replace("-", "_")
    return is_secret_param_name(low) or "pass" in low


def command_secret_reason(command: str) -> str | None:
    """Why *command* looks like it holds a secret, or ``None``."""
    names = _ASSIGNED_NAME.findall(command) + _FLAG_NAME.findall(command)
    named = sorted({name for name in names if _secret_name(name)})
    if named:
        return f"the command passes a value for {', '.join(named)}"
    if any(form.search(command) for form in _SECRET_FORMS):
        return "the command passes a password on the command line"
    redacted = redact({"command": command}, get_default_policy()).redacted
    if isinstance(redacted, dict) and redacted.get("command") != command:
        return "the command holds text the secret filter would hide"
    return None


def _target_expression(params: dict[str, Any]) -> dict[str, Any]:
    """The target as the caller gave it, with the defaults filled in."""
    if params.get("tgt_uuid"):
        return {"tgt_uuid": str(params["tgt_uuid"])}
    return {
        "master_id": str(params.get("master_id") or "*"),
        "tgt_type": str(params.get("tgt_type") or "glob"),
        "tgt": str(params["tgt"]),
    }


def _resolve_kwarg(expression: dict[str, Any]) -> dict[str, Any]:
    if "tgt_uuid" in expression:
        return {"tgt_uuid": expression["tgt_uuid"]}
    return {
        "tgt": {
            expression["master_id"]: {
                "tgt": expression["tgt"],
                "tgt_type": expression["tgt_type"],
            }
        }
    }


def _minion_ids(value: Any) -> list[str]:
    """The minion ids under one master in a ``get_target_group_minions`` answer."""
    if isinstance(value, dict):
        inner = value.get("minions")
        value = inner if isinstance(inner, list) else list(value)
    if not isinstance(value, list):
        return []
    ids = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("minion_id") or item.get("minion") or item.get("id")
        if isinstance(item, str) and item:
            ids.append(item)
    return ids


def _matched_pairs(ret: Any) -> list[tuple[str, str]]:
    if not isinstance(ret, dict):
        return []
    pairs = {
        (str(master_id), minion_id)
        for master_id, value in ret.items()
        for minion_id in _minion_ids(value)
    }
    return sorted(pairs)


async def _resolve_minions(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    expression: dict[str, Any],
) -> list[tuple[str, str]]:
    """The ``(master_id, minion_id)`` pairs the target matches today. Raises to refuse."""
    ret = await connector.rpc(target, operator, TARGET_MINIONS_RPC, _resolve_kwarg(expression))
    pairs = _matched_pairs(ret)
    if not pairs:
        raise SaltChangeRefusedError(
            "Nothing was started: the target matches no minion. salt.minion.list and "
            "salt.target.list show the minions and the saved targets."
        )
    if len(pairs) > MAX_RUN_MINIONS:
        raise SaltChangeRefusedError(
            f"Nothing was started: the target matches {len(pairs)} minions; MEHO runs on "
            f"at most {MAX_RUN_MINIONS} at once. Narrow the target."
        )
    odd = [
        f"{minion} on {master}"
        for master, minion in pairs
        if not _PLAIN_ID.fullmatch(master + minion)
    ]
    if odd:
        raise SaltChangeRefusedError(
            "Nothing was started: these matched ids have a comma, a space or a glob "
            f"character, so MEHO cannot name them one by one: {', '.join(odd[:10])}."
        )
    return pairs


def minions_sha256(pairs: list[tuple[str, str]]) -> str:
    """A hash of the matched minions, in a fixed order."""
    return hashlib.sha256(json.dumps(sorted(pairs)).encode()).hexdigest()


def _list_target(pairs: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    """A ``list`` target per master that names exactly *pairs*."""
    by_master: dict[str, list[str]] = defaultdict(list)
    for master_id, minion_id in pairs:
        by_master[master_id].append(minion_id)
    return {
        master_id: {"tgt": ",".join(sorted(minions)), "tgt_type": "list"}
        for master_id, minions in sorted(by_master.items())
    }


def _state_call(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``state.apply`` and its ``arg``: the state names as ``mods``, or none for a highstate."""
    arg: dict[str, Any] = {}
    kwarg: dict[str, Any] = {}
    states = params.get("states") or []
    if states:
        kwarg["mods"] = ",".join(str(state) for state in states)
    if params.get("test") is True:
        kwarg["test"] = True
    if params.get("saltenv"):
        kwarg["saltenv"] = str(params["saltenv"])
    if kwarg:
        arg["kwarg"] = kwarg
    return "state.apply", arg


def _command_call(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``cmd.run`` and its ``arg``: the command text. Raises when it looks like a secret."""
    command = str(params["command"])
    reason = command_secret_reason(command)
    if reason is not None:
        raise SaltChangeRefusedError(
            f"Nothing was started: {reason}. Do not put a secret into a command: keep it "
            "in pillar data or in the Salt server's own secret store, and read it from there."
        )
    return "cmd.run", {"kwarg": {"cmd": command}}


def _card(
    fun: str, arg: dict[str, Any], expression: dict[str, Any], pairs: list[tuple[str, str]]
) -> dict[str, Any]:
    return {
        "blast_radius": {
            "object": {
                "kind": "salt_job",
                "fun": fun,
                "arg": arg,
                "target": expression,
                "minion_count": len(pairs),
                "minions_sha256": minions_sha256(pairs),
            },
            "children": [
                {"kind": "minion", "master_id": master_id, "minion_id": minion_id}
                for master_id, minion_id in pairs
            ],
            "irreversibility": _RUN_IRREVERSIBILITY,
        }
    }


def _call_for(op_id: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _state_call(params) if op_id == RUN_STATE_OP_ID else _command_call(params)


def _run_preview(op_id: str) -> Any:
    async def _builder(ctx: PreviewContext) -> dict[str, Any] | None:
        """Preview builder: resolve the target and name every minion. Raises to refuse."""
        from meho_backplane.connectors.salt.connector import SaltConnector

        connector = ctx.connector_instance
        if not isinstance(connector, SaltConnector) or ctx.target is None:
            return None
        fun, arg = _call_for(op_id, ctx.params)
        expression = _target_expression(ctx.params)
        pairs = await _resolve_minions(connector, ctx.operator, ctx.target, expression)
        return _card(fun, arg, expression, pairs)

    return _builder


def _drift(op_id: str, card: dict[str, Any]) -> str | None:
    """Why the run now differs from the approval card, or ``None``."""
    seen = approved_preview(op_id)
    seen_object = ((seen or {}).get("blast_radius") or {}).get("object") or {}
    if not seen_object.get("minions_sha256"):
        return "the approval holds no minion list to compare with. Preview and ask again."
    now = card["blast_radius"]["object"]
    for field in ("fun", "arg", "target"):
        if seen_object.get(field) != now[field]:
            return f"the {field} differs from what the approver saw. Preview and ask again."
    if seen_object["minions_sha256"] != now["minions_sha256"]:
        return (
            f"the target matched {seen_object.get('minion_count')} minions when it was "
            f"approved and matches a different list of {now['minion_count']} minions now. "
            "Preview and ask again."
        )
    return None


async def _running_job(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    fun: str,
    list_target: dict[str, Any],
) -> dict[str, Any] | None:
    """A job of *fun* that has not ended and runs on the same minions, if RaaS lists one."""
    kwarg = {"state": list(RUNNING_JOB_STATES), "fun": fun, "cmd": "local", "limit": 50}
    rows, _ = _results(await connector.rpc(target, operator, CMDS_RPC, kwarg))
    return next((row for row in rows if row.get("tgt") == list_target), None)


def _start_answer(
    status: str, fun: str, minion_count: int, *, jid: Any = None, reason: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "started": status == "accepted",
        "jid": jid if isinstance(jid, str) and jid else None,
        "fun": fun,
        "minion_count": minion_count,
        "status_read": STATUS_READ,
        "reason": reason,
    }


async def _start(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
    op_id: str,
) -> dict[str, Any]:
    fun, arg = _call_for(op_id, params)
    expression = _target_expression(params)
    pairs = await _resolve_minions(connector, operator, target, expression)
    drift = _drift(op_id, _card(fun, arg, expression, pairs))
    if drift is not None:
        return _start_answer(
            "precondition_failed", fun, len(pairs), reason=f"Nothing was started: {drift}"
        )
    list_target = _list_target(pairs)
    running = await _running_job(connector, operator, target, fun, list_target)
    if running is not None:
        return _start_answer(
            "already_running",
            fun,
            len(pairs),
            jid=running.get("jid"),
            reason=(
                f"A {fun} job that has not ended runs on the same minions. Nothing was "
                f"started; follow that job with {STATUS_READ}."
            ),
        )
    kwarg: dict[str, Any] = {"cmd": "local", "fun": fun, "tgt": list_target}
    if arg:
        kwarg["arg"] = arg
    jid = await connector.rpc(target, operator, ROUTE_CMD_RPC, kwarg)
    reason = (
        None
        if isinstance(jid, str) and jid
        else (
            "RaaS accepted the job but returned no jid. Find it with salt.job.list "
            f"(fun {fun}) before you start anything again."
        )
    )
    return _start_answer("accepted", fun, len(pairs), jid=jid, reason=reason)


async def run_state_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.job.run_state`` -- ``state.apply`` on the approved minions."""
    return await _start(connector, operator, target, params, RUN_STATE_OP_ID)


async def run_command_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.job.run_command`` -- ``cmd.run`` on the approved minions."""
    return await _start(connector, operator, target, params, RUN_COMMAND_OP_ID)


async def job_cancel_impl(
    connector: SaltConnector,
    operator: Operator,
    target: SaltTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``salt.job.cancel`` -- read the job; stop it with ``cmd.stop_job`` when it runs."""
    jid = str(params["jid"])
    rows, _ = _results(await connector.rpc(target, operator, CMDS_RPC, {"jid": jid, "limit": 1}))
    row = next((item for item in rows if item.get("jid") == jid), None)
    if row is None:
        raise SaltChangeRefusedError(
            f"Nothing was stopped: RaaS has no job run with jid {jid!r}. salt.job.list "
            "shows the runs."
        )
    state = row.get("state")
    answer: dict[str, Any] = {
        "jid": jid,
        "job_state": state,
        "job_status": JOB_STATUS_BY_STATE.get(str(state), "unknown"),
        "stop_jid": None,
        "status_read": STATUS_READ,
    }
    if state == "stopping":
        return {**answer, "status": "already_stopping", "stop_requested": False}
    if state not in RUNNING_JOB_STATES:
        return {**answer, "status": "not_running", "stop_requested": False}
    stop_jid = await connector.rpc(target, operator, STOP_JOB_RPC, {"jid": jid})
    return {
        **answer,
        "status": "accepted",
        "stop_requested": True,
        "stop_jid": stop_jid if isinstance(stop_jid, str) and stop_jid else None,
    }


register_preview_builder(RUN_STATE_OP_ID, _run_preview(RUN_STATE_OP_ID))
register_preview_builder(RUN_COMMAND_OP_ID, _run_preview(RUN_COMMAND_OP_ID))
