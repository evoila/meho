# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``holodeck.instance.remove``: delete one nested lab instance that Holodeck built.

The deploy ops in :mod:`.ops_deploy` build an instance. This module adds the
matching delete, so a disposable lab can be built and deleted through MEHO.

How a call runs:

1. **Check.** One read-only PowerShell script on the HoloRouter checks that
   ``Remove-HoloDeckInstance`` exists with ``-InstanceID`` (added in Holodeck
   9.0.1) and, when asked, ``-ResetHoloRouter``; that ``Get-HoloDeckInstance``
   exists (9.0.2); reads the instance with ``Get-HoloDeckInstance -InstanceID``;
   lists the instance's files under ``/holodeck-runtime/config``, ``output``
   and ``state`` (a file whose name holds the instance id as a whole word, or
   whose text holds it as a JSON string); and reads the instance's JSON state
   files. The same script feeds the approval preview.
2. **Decide.** A missing cmdlet or switch is ``refused`` with a version
   message. An instance that ``Get-HoloDeckInstance`` does not list is
   ``unchanged``: nothing is deleted. A listed instance whose answer and state
   files name no VM is ``refused``: MEHO does not delete what it cannot show.
3. **Run.** The delete runs detached in a ``tmux`` session, the same way as
   ``holodeck.instance.start``: load the instance's config
   (``Import-HoloDeckConfig``), run ``Remove-HoloDeckInstance -InstanceID``,
   list the VMs whose name still starts with ``<instance-id>-``, then move
   the instance's files into ``/holodeck-runtime/removed/<id>-<time>/``. The
   script writes its progress to ``/holodeck-runtime/logs/<id>-remove.json``;
   ``holodeck.instance.status`` reads that file and reports ``leftover_vms``.
4. **Finish an interrupted run.** With ``-ResetHoloRouter`` the router
   reboots at the end of the cmdlet and kills the script before the last two
   steps. The progress file then still says ``running`` while the ``tmux``
   session is gone. ``holodeck.instance.status`` reports that, and a second
   ``holodeck.instance.remove`` call with the same id runs only the two
   missing steps (no cmdlet) and finishes the progress file.

No password or token is a parameter, and none is returned. The script reads
the vCenter login from the loaded config on the appliance, by variable name
only. The ``Get-HoloDeckInstance`` answer and the state files are never
passed on as is: MEHO reads only known VM name fields, skips every field
whose name says it is a secret, and keeps only VM names and where they run.

Vendor references: the Holodeck 9.0 guide (``Remove-HoloDeckInstance
[-ResetHoloRouter]``) and the Holodeck release notes (9.0.1: ``-InstanceID``
and a 15 second abort window; 9.0.2: ``Get-HoloDeckInstance``).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import PwshRunError, ps_single_quote, pwsh_run
from meho_backplane.connectors.holodeck._response_schemas import HOLODECK_RESPONSE_SCHEMAS
from meho_backplane.connectors.holodeck.ops import SSH_TRANSPORT_NOTE, HolodeckOp
from meho_backplane.connectors.holodeck.ops_deploy import _pwsh_encoded, target_hr_version
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

__all__ = [
    "HOLODECK_WHEN_TO_USE_REMOVE_BY_GROUP",
    "INSTANCE_ID_PATTERN",
    "REMOVE_OPS",
    "file_name_pattern",
    "holodeck_instance_remove",
    "instance_vms",
    "interrupted",
    "known_vms",
    "read_remove_record",
    "remove_record_path",
]

_OP_ID = "holodeck.instance.remove"

#: Same pattern as ``holodeck.instance.start``'s ``instance_id``.
INSTANCE_ID_PATTERN = r"^[A-Za-z0-9]{1,8}$"
_INSTANCE_ID_RE = re.compile(INSTANCE_ID_PATTERN)

_RUNTIME = "/holodeck-runtime"
_STATE_DIRS: tuple[str, ...] = (f"{_RUNTIME}/config", f"{_RUNTIME}/output", f"{_RUNTIME}/state")

#: Keys in a ``Get-HoloDeckInstance`` record or a state file that may hold a VM
#: name, and keys that may say where the VM runs. Matched without case.
_NAME_KEYS: tuple[str, ...] = ("name", "vmname", "vm_name", "hostname", "fqdn")
_LOCATION_KEYS: tuple[str, ...] = ("host", "esxhost", "vmhost", "cluster", "datastore", "location")
#: A key whose name holds one of these words is a secret. MEHO never reads its value.
_SECRET_KEY_WORDS: tuple[str, ...] = ("password", "passwd", "pwd", "secret", "token", "credential")

#: Line that separates the progress file from the tmux session list in
#: :func:`read_remove_record`.
_TMUX_MARKER = "__tmux_sessions__"

_NO_VMS_REASON = (
    "Get-HoloDeckInstance lists this instance, but neither its answer nor the "
    "instance's state file names a VM, so MEHO cannot show what it would delete. "
    "Nothing was deleted. Delete the instance by hand."
)


def remove_record_path(instance_id: str) -> str:
    """Return the progress file the detached remove writes for *instance_id*."""
    return f"{_RUNTIME}/logs/{instance_id}-remove.json"


def _tmux_session(instance_id: str) -> str:
    return f"holodeck-remove-{instance_id}"


# ---------------------------------------------------------------------------
# The read-only check script (handler and preview)
# ---------------------------------------------------------------------------


def file_name_pattern(instance_id: str) -> str:
    """Return the regex that finds *instance_id* as a whole word in a file name.

    A plain "contains" match would take ``lab01-state.json`` for instance
    ``lab0``, so the id must not touch another letter or digit. The pattern
    reads the same in .NET (PowerShell ``-match``) and in Python ``re``.
    """
    return f"(^|[^A-Za-z0-9]){re.escape(instance_id)}([^A-Za-z0-9]|$)"


def _check_script(instance_id: str) -> str:
    """Compose the read-only check script. *instance_id* is already validated."""
    quoted_id = ps_single_quote(instance_id)
    name_pattern = ps_single_quote(file_name_pattern(instance_id))
    dirs = ", ".join(ps_single_quote(d) for d in _STATE_DIRS)
    config_dir = ps_single_quote(_STATE_DIRS[0])
    state_dir = ps_single_quote(_STATE_DIRS[2])
    return (
        "$Global:ProgressPreference = 'SilentlyContinue'; "
        "$Global:WarningPreference = 'SilentlyContinue'; "
        f"$id = {quoted_id}; "
        "$rm = Get-Command Remove-HoloDeckInstance -ErrorAction SilentlyContinue; "
        "$get = Get-Command Get-HoloDeckInstance -ErrorAction SilentlyContinue; "
        "$out = [ordered]@{ "
        "has_remove = [bool]$rm; "
        "has_instance_param = [bool]($rm -and $rm.Parameters.ContainsKey('InstanceID')); "
        "has_reset_param = [bool]($rm -and $rm.Parameters.ContainsKey('ResetHoloRouter')); "
        "has_get = [bool]$get; instance = $null; lookup_error = $null; "
        "config_ids = @(); files = @(); state = @() }; "
        "if ($get) { try { $out.instance = Get-HoloDeckInstance -InstanceID $id "
        "-ErrorAction Stop } catch { $out.lookup_error = $_.Exception.Message } }; "
        "$needle = '\"' + $id + '\"'; "
        f"$word = {name_pattern}; "
        f"foreach ($d in @({dirs})) {{ if (Test-Path -LiteralPath $d) {{ "
        "foreach ($f in @(Get-ChildItem -LiteralPath $d -File -Recurse "
        "-ErrorAction SilentlyContinue)) { "
        "if ($f.Name -match $word -or (Select-String -LiteralPath $f.FullName "
        "-SimpleMatch -Quiet -Pattern $needle)) { $out.files += $f.FullName; "
        f"if ($d -eq {config_dir} -and $f.Extension -eq '.json') "
        "{ $out.config_ids += $f.BaseName }; "
        f"if ($d -eq {state_dir} -and $f.Extension -eq '.json') {{ try {{ "
        "$out.state += ,(Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json) } "
        "catch { } } } } } }; "
        "$out | ConvertTo-Json -Depth 8 -Compress"
    )


async def _run_check(
    connector: HolodeckConnector,
    target: Any,
    instance_id: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run the check script and return its answer as a dict.

    Raises :class:`PwshRunError` when PowerShell fails or the answer is not
    a JSON object.
    """
    payload = await pwsh_run(connector, target, _check_script(instance_id), operator=operator)
    if not isinstance(payload, dict):
        raise PwshRunError("the check script returned no JSON object", exit_status=None, stderr="")
    return payload


def _as_list(value: Any) -> list[Any]:
    """``ConvertTo-Json`` writes a one-item array as a bare value and none as null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _found(check: dict[str, Any]) -> bool:
    """True when ``Get-HoloDeckInstance`` listed the instance."""
    return bool(_as_list(check.get("instance")))


def _version_problem(check: dict[str, Any], *, reset_router: bool, hr_version: str | None) -> str:
    """Return why this toolkit cannot run the remove, or ``""`` when it can."""
    seen = f" (this HoloRouter reports version {hr_version})" if hr_version else ""
    if not check.get("has_remove") or not check.get("has_instance_param"):
        return (
            "this Holodeck toolkit has no 'Remove-HoloDeckInstance -InstanceID'" + seen + ". "
            "It came with Holodeck 9.0.1. Update the HoloRouter, or delete the instance by hand."
        )
    if not check.get("has_get"):
        return (
            "this Holodeck toolkit has no 'Get-HoloDeckInstance'" + seen + ", so MEHO cannot "
            "check what it would delete. It came with Holodeck 9.0.2. Update the HoloRouter."
        )
    if reset_router and not check.get("has_reset_param"):
        return (
            "this Holodeck toolkit has no '-ResetHoloRouter' switch on "
            "Remove-HoloDeckInstance" + seen + ". Call again with reset_router false."
        )
    return ""


def _blocker(check: dict[str, Any], *, reset_router: bool, hr_version: str | None) -> str:
    """Return why the remove must not run (old toolkit, failed read), or ``""``."""
    problem = _version_problem(check, reset_router=reset_router, hr_version=hr_version)
    if problem:
        return problem
    if check.get("lookup_error"):
        return f"Get-HoloDeckInstance failed: {check['lookup_error']}"
    return ""


def _answer(status: str, instance_id: str, reason: str) -> dict[str, Any]:
    """A ``refused`` or ``unchanged`` answer with its reason."""
    return {"status": status, "instance_id": instance_id, "reason": reason}


def _is_secret_key(key: str) -> bool:
    return any(word in key for word in _SECRET_KEY_WORDS)


def _location(fields: dict[str, Any]) -> str | None:
    for key in _LOCATION_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def instance_vms(record: Any, instance_id: str) -> list[dict[str, Any]]:
    """Return the VMs named in a ``Get-HoloDeckInstance`` answer or a state file.

    Neither has a published shape, so MEHO walks the objects and reads only
    the known name fields (``_NAME_KEYS``) whose value starts with
    ``<instance-id>-`` (Holodeck names each nested VM that way), with where
    the VM runs when the same object says so in a text field. A field whose
    name says it is a secret (password, token, ...) is skipped with everything
    under it, and a text outside a name field is never a VM. So a password
    that happens to start with the instance id cannot leak into a result.
    """
    prefix = f"{instance_id.lower()}-"
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            fields = {
                str(k).lower(): v for k, v in node.items() if not _is_secret_key(str(k).lower())
            }
            for key in _NAME_KEYS:
                name = fields.get(key)
                if isinstance(name, str) and name.lower().startswith(prefix):
                    found.setdefault(name, {"name": name, "location": _location(fields)})
                    break
            for value in fields.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(record)
    return sorted(found.values(), key=lambda vm: vm["name"])


def known_vms(check: dict[str, Any], instance_id: str) -> list[dict[str, Any]]:
    """The VMs of the instance, from the ``Get-HoloDeckInstance`` answer and its state files.

    The answer wins when both name the same VM.
    """
    found = {vm["name"]: vm for vm in instance_vms(check.get("state"), instance_id)}
    found.update({vm["name"]: vm for vm in instance_vms(check.get("instance"), instance_id)})
    return sorted(found.values(), key=lambda vm: vm["name"])


def interrupted(record: dict[str, Any] | None) -> bool:
    """True when a remove record says ``running`` but its ``tmux`` session is gone.

    That happens when the HoloRouter reboots (``reset_router``) before the
    script lists the leftover VMs and moves the files. A record that holds the
    cmdlet's error is not resumed: the cmdlet failed, there is nothing to
    finish.
    """
    return (
        record is not None
        and record.get("state") == "running"
        and record.get("tmux_alive") is False
        and not record.get("error")
    )


# ---------------------------------------------------------------------------
# The detached remove
# ---------------------------------------------------------------------------


def _remove_script(
    *,
    instance_id: str,
    config_id: str,
    reset_router: bool,
    files: list[str],
    resume_of: dict[str, Any] | None = None,
) -> str:
    """Compose the PowerShell that runs in ``tmux``. Every value is quoted.

    Order: write the progress file, load the config, run the cmdlet, write the
    file again (so the cmdlet's error survives a reboot), list the VMs left in
    vCenter, move the instance's files (only when the cmdlet did not fail),
    write the file a last time. ``Get-VM`` stops on error, so a failed read
    keeps ``leftover_vms`` null and sets ``leftover_error``; an empty search
    gives ``[]``. ``Move-Item`` stops on error too: a file that did not move
    goes to ``move_errors``, and only moved files go to ``moved_files``.

    With *resume_of* (the progress file of an interrupted run) the script
    finishes that run instead: it skips the cmdlet and keeps the old start
    time and ``reset_router`` value.
    """
    resuming = resume_of is not None
    now = "(Get-Date).ToUniversalTime().ToString('o')"
    started = now
    if resume_of is not None:
        reset_router = bool(resume_of.get("reset_router"))
        old_start = resume_of.get("started_at")
        if isinstance(old_start, str) and old_start:
            started = ps_single_quote(old_start)
    reset_value = "$true" if reset_router else "$false"
    resumed_value = "$true" if resuming else "$false"
    load = f"$null = Import-HoloDeckConfig -ConfigID {ps_single_quote(config_id)}; "
    if resuming:
        cmdlet = ""
        leftover_load = load
    else:
        reset_flag = " -ResetHoloRouter" if reset_router else ""
        cmdlet = (
            f"try {{ {load}Remove-HoloDeckInstance -InstanceID $id{reset_flag} "
            "-ErrorAction Stop } catch { $r.error = $_.Exception.Message }; Save-Record; "
        )
        leftover_load = ""
    file_list = ", ".join(ps_single_quote(path) for path in files)
    move = (
        f"if (-not $r.error -and @({file_list}).Count -gt 0) {{ "
        f"$dest = {ps_single_quote(f'{_RUNTIME}/removed/{instance_id}-')} "
        "+ (Get-Date -Format 'yyyyMMdd-HHmmss'); "
        f"foreach ($p in @({file_list})) {{ if (Test-Path -LiteralPath $p) {{ try {{ "
        f"$sub = Join-Path $dest (Split-Path -Parent $p).Substring({len(_RUNTIME) + 1}); "
        "$null = [System.IO.Directory]::CreateDirectory($sub); "
        "Move-Item -LiteralPath $p -Destination $sub -Force -ErrorAction Stop; "
        "$r.moved_files += $p } "
        "catch { $r.move_errors += ($p + ': ' + $_.Exception.Message) } } }; "
        "if ($r.moved_files.Count -gt 0) { $r.moved_to = $dest } }; "
        if files
        else ""
    )
    return (
        "$Global:ProgressPreference = 'SilentlyContinue'; "
        "$Global:WarningPreference = 'SilentlyContinue'; "
        f"$id = {ps_single_quote(instance_id)}; "
        f"$rec = {ps_single_quote(remove_record_path(instance_id))}; "
        "$r = [ordered]@{ instance_id = $id; state = 'running'; "
        f"started_at = {started}; finished_at = $null; "
        f"reset_router = {reset_value}; resumed = {resumed_value}; error = $null; "
        "leftover_vms = $null; leftover_error = $null; moved_to = $null; "
        "moved_files = @(); move_errors = @() }; "
        "function Save-Record { $r | ConvertTo-Json -Depth 4 -Compress "
        "| Set-Content -LiteralPath $rec }; "
        "Save-Record; "
        f"{cmdlet}"
        f"try {{ {leftover_load}"
        "if (-not $global:DefaultVIServers -or $global:DefaultVIServers.Count -eq 0) { "
        "$c = $Global:config.Target; "
        "$pw = ConvertTo-SecureString -AsPlainText -Force -String $c.password; "
        "$cred = New-Object System.Management.Automation.PSCredential($c.username, $pw); "
        "$null = Connect-VIServer -Server $c.hostname -Credential $cred -ErrorAction Stop }; "
        "$r.leftover_vms = @(Get-VM -Name ($id + '-*') -ErrorAction Stop "
        "| ForEach-Object { $_.Name }) } "
        "catch { $r.leftover_error = $_.Exception.Message }; "
        f"{move}"
        "$r.state = $(if ($r.error) { 'failed' } else { 'done' }); "
        f"$r.finished_at = {now}; "
        "Save-Record"
    )


def _pick_config_id(params: dict[str, Any], check: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(config_id, "")`` or ``(None, reason)``."""
    given = params.get("config_id")
    if isinstance(given, str) and given:
        return given, ""
    candidates = sorted({str(c) for c in _as_list(check.get("config_ids")) if c})
    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, (
            "no config under /holodeck-runtime/config names this instance, and "
            "Remove-HoloDeckInstance needs the config loaded. Pass config_id."
        )
    return None, (
        f"more than one config names this instance ({', '.join(candidates)}). Pass config_id."
    )


async def _launch(
    self: HolodeckConnector, target: Any, instance_id: str, script: str, operator: Operator | None
) -> str:
    """Start *script* detached in ``tmux``. Returns ``""``, or why it failed."""
    log_path = f"{_RUNTIME}/logs/{instance_id}-remove.log"
    remote = (
        f"pwsh -NoProfile -NonInteractive -EncodedCommand {_pwsh_encoded(script)} "
        f"> {shlex.quote(log_path)} 2>&1"
    )
    session = shlex.quote(_tmux_session(instance_id))
    launch = f"tmux new-session -d -s {session} {shlex.quote(remote)}"
    try:
        proc = await self._run_command(target, launch, operator=operator)
    except Exception as exc:
        return str(exc)
    if getattr(proc, "exit_status", 0) not in (0, None):
        stderr = str(getattr(proc, "stderr", "") or "").strip()[:4096]
        return f"could not start the remove in tmux: {stderr or 'no error text'}"
    return ""


async def holodeck_instance_remove(
    self: HolodeckConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Delete one Holodeck instance: check, then start the remove in ``tmux``.

    Op-id: ``holodeck.instance.remove``. Runs only after a person approved
    the call. Returns ``status`` ``started``, ``unchanged`` (no such instance)
    or ``refused`` (with ``reason``). A second call for an instance whose
    remove was interrupted finishes it (``resumed`` true). See the module
    docstring for the steps.
    """
    instance_id = str(params.get("instance_id", ""))
    if not _INSTANCE_ID_RE.fullmatch(instance_id):
        return _answer("refused", instance_id, "instance_id must be 1 to 8 letters or digits")
    reset_router = bool(params.get("reset_router", False))
    move_state_files = bool(params.get("move_state_files", True))

    try:
        check = await _run_check(self, target, instance_id, operator)
    except PwshRunError as exc:
        return _answer("refused", instance_id, str(exc))
    blocker = _blocker(check, reset_router=reset_router, hr_version=target_hr_version(target))
    if blocker:
        return _answer("refused", instance_id, blocker)
    found = _found(check)
    record = None if found else await read_remove_record(self, target, instance_id, operator)
    resume = interrupted(record)
    if not found and not resume:
        return _answer(
            "unchanged",
            instance_id,
            "Get-HoloDeckInstance does not list this instance; nothing was deleted",
        )
    vms = known_vms(check, instance_id) if found else []
    if found and not vms:
        return _answer("refused", instance_id, _NO_VMS_REASON)
    config_id, reason = _pick_config_id(params, check)
    if config_id is None:
        return _answer("refused", instance_id, reason)

    files = [str(f) for f in _as_list(check.get("files")) if f] if move_state_files else []
    script = _remove_script(
        instance_id=instance_id,
        config_id=config_id,
        reset_router=reset_router,
        files=files,
        resume_of=record if resume else None,
    )
    failure = await _launch(self, target, instance_id, script, operator)
    if failure:
        return _answer("refused", instance_id, failure)

    return {
        "status": "started",
        "instance_id": instance_id,
        "config_id": config_id,
        "reset_router": reset_router and not resume,
        "move_state_files": move_state_files,
        "resumed": resume,
        "vms": [vm["name"] for vm in vms],
        "files_to_move": files,
        "start_marker": {
            "tmux_session": _tmux_session(instance_id),
            "log_path": f"{_RUNTIME}/logs/{instance_id}-remove.log",
            "record_path": remove_record_path(instance_id),
        },
    }


async def read_remove_record(
    connector: HolodeckConnector,
    target: Any,
    instance_id: str,
    operator: Operator | None,
) -> dict[str, Any] | None:
    """Read the progress file of a remove, or ``None`` when there is none.

    Used by ``holodeck.instance.status`` and by the remove op itself. The
    same command lists the ``tmux`` sessions, so the record gets a
    ``tmux_alive`` flag: ``False`` when the remove's session is gone (the
    script was killed, for example by the router reboot), ``True`` when it
    still runs, ``None`` when the list is missing. An id that does not match
    the instance id pattern has no progress file, so nothing is read.
    """
    if not _INSTANCE_ID_RE.fullmatch(instance_id):
        return None
    cmd = (
        f"cat {shlex.quote(remove_record_path(instance_id))} && {{ echo; echo {_TMUX_MARKER}; "
        "tmux list-sessions -F '#S' 2>/dev/null; true; }"
    )
    try:
        proc = await connector._run_command(target, cmd, operator=operator)
    except Exception:
        return None
    if getattr(proc, "exit_status", 0) not in (0, None):
        return None
    stdout = getattr(proc, "stdout", "")
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    text, marker, sessions = stdout.partition(f"\n{_TMUX_MARKER}\n")
    try:
        record = json.loads(text.lstrip("﻿"))
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    record["tmux_alive"] = _tmux_session(instance_id) in sessions.splitlines() if marker else None
    return record


# ---------------------------------------------------------------------------
# Approval preview (blast radius)
# ---------------------------------------------------------------------------


async def _instance_remove_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """List what the approved call deletes: the VMs, the router reset, the files.

    Read-only: it runs the same check script as the handler. When the toolkit
    cannot run the remove, the check fails, or the instance is listed but no
    VM can be named, it raises, so the call does not wait for approval and
    the caller reads why. A call that only finishes an interrupted remove
    lists the files to move and says that no VM is deleted.
    """
    from meho_backplane.connectors.holodeck.connector import HolodeckConnector

    connector = ctx.connector_instance
    instance_id = ctx.params.get("instance_id")
    if not isinstance(connector, HolodeckConnector) or ctx.target is None:
        return None
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
        return None
    reset_router = bool(ctx.params.get("reset_router", False))
    move_state_files = bool(ctx.params.get("move_state_files", True))

    check = await _run_check(connector, ctx.target, instance_id, ctx.operator)
    blocker = _blocker(check, reset_router=reset_router, hr_version=target_hr_version(ctx.target))
    if blocker:
        raise ValueError(blocker)

    found = _found(check)
    record = None
    if not found:
        record = await read_remove_record(connector, ctx.target, instance_id, ctx.operator)
    resume = interrupted(record)
    children: list[dict[str, Any]] = []
    files: list[str] = []
    if found:
        vms = known_vms(check, instance_id)
        if not vms:
            raise ValueError(_NO_VMS_REASON)
        children = [{"kind": "vm", "name": vm["name"], "location": vm["location"]} for vm in vms]
        if reset_router:
            children.append(
                {
                    "kind": "router_reset",
                    "name": "HoloRouter",
                    "effect": "removes the instance's networking and reboots the HoloRouter",
                }
            )
    if (found or resume) and move_state_files:
        files = [str(f) for f in _as_list(check.get("files")) if f]
        children.extend({"kind": "file_move", "path": path} for path in files)
    if found:
        note = "The instance's nested VMs are deleted and cannot be restored."
    elif resume:
        note = (
            "An earlier remove of this instance was interrupted. This call deletes no "
            "VM: it lists the VMs that are left and moves the instance's files."
        )
    else:
        note = "Get-HoloDeckInstance does not list this instance; the call deletes nothing."
    return {
        "blast_radius": {
            "object": {
                "kind": "holodeck_instance",
                "instance_id": instance_id,
                "found": found,
                "resume": resume,
            },
            "children": children,
            "irreversibility": "permanent",
        },
        "reset_router": reset_router and found,
        "files_to_move": files,
        "note": note,
    }


register_preview_builder(_OP_ID, _instance_remove_preview)


# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

#: Group text. The key has the ``deploy-`` prefix like the other deploy groups.
HOLODECK_WHEN_TO_USE_REMOVE_BY_GROUP: dict[str, str] = {
    "deploy-remove": (
        "Deleting a Holodeck lab: holodeck.instance.remove deletes the nested ESX "
        "hosts and the installer VM of one instance (Remove-HoloDeckInstance), can "
        "reset the HoloRouter, and moves the instance's state files aside. A person "
        "must approve each call. Then holodeck.instance.status shows the VMs that "
        "are left."
    ),
}

_INSTANCE_REMOVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "instance_id": {
            "type": "string",
            "pattern": INSTANCE_ID_PATTERN,
            "description": (
                "Instance id to delete, 1 to 8 letters or digits, the same id as "
                "in holodeck.instance.start."
            ),
        },
        "reset_router": {
            "type": "boolean",
            "default": False,
            "description": (
                "Also remove the instance's networking on the HoloRouter and reboot "
                "it (-ResetHoloRouter). Default false."
            ),
        },
        "move_state_files": {
            "type": "boolean",
            "default": True,
            "description": (
                "Move the instance's files under /holodeck-runtime/config, output and "
                "state into /holodeck-runtime/removed/<id>-<time>, so a later deploy "
                "starts clean. Default true."
            ),
        },
        "config_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Config to load before the delete. Leave it out to use the one "
                "config that names this instance."
            ),
        },
    },
    "required": ["instance_id"],
    "additionalProperties": False,
    "examples": [{"instance_id": "lab01", "reset_router": False, "move_state_files": True}],
}


REMOVE_OPS: tuple[HolodeckOp, ...] = (
    HolodeckOp(
        op_id=_OP_ID,
        handler_attr="instance_remove",
        summary="Delete one Holodeck lab instance: its nested ESX hosts and installer VM.",
        description=(
            "Runs Remove-HoloDeckInstance -InstanceID in a tmux session on the "
            "HoloRouter, optionally with -ResetHoloRouter, then lists the VMs that "
            "are left and moves the instance's state files aside. Cannot be undone. "
            "A person must approve each call."
        ),
        parameter_schema=_INSTANCE_REMOVE_SCHEMA,
        response_schema=HOLODECK_RESPONSE_SCHEMAS[_OP_ID],
        group_key="deploy-remove",
        tags=("write", "deploy", "delete", "holodeck"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call to delete a nested lab that holodeck.instance.start built, for "
                "example after a test. The approval preview lists the VMs, whether "
                "the router is reset and rebooted, and which files move. An instance "
                "that Get-HoloDeckInstance does not list is left alone (status "
                "unchanged). " + SSH_TRANSPORT_NOTE
            ),
            "preconditions": (
                "A person must approve the call. No deploy may run on the HoloRouter; "
                "check with holodeck.instance.status. The HoloRouter needs Holodeck "
                "9.0.2 or later. MEHO must be able to name the instance's VMs from "
                "Get-HoloDeckInstance or the instance's state file; otherwise the call "
                "is refused. With reset_router true the router reboots, and the steps "
                "after the delete (leftover VMs, file move) may not finish; "
                "holodeck.instance.status then says so, and a second call with the "
                "same instance_id finishes them."
            ),
            "parameter_hints": {
                "instance_id": "The id used at deploy time; every nested VM name starts with it.",
                "reset_router": "Only when no other instance uses the HoloRouter.",
                "config_id": "Only when MEHO cannot find the config of the instance itself.",
            },
            "output_shape": (
                "{status, instance_id, reason?, config_id?, reset_router?, "
                "move_state_files?, resumed?, vms[]?, files_to_move[]?, start_marker?: "
                "{tmux_session, log_path, record_path}}. 'status' is started, "
                "unchanged (no such instance) or refused (see 'reason', for example "
                "a toolkit that is too old). 'resumed' true means the call only "
                "finishes an interrupted remove."
            ),
            "next_step": (
                "Poll holodeck.instance.status with the same instance_id until status "
                "is removed. Delete each name in its leftover_vms with "
                "vmware.composite.vm.destroy. When a note says the remove was "
                "interrupted, call holodeck.instance.remove again with the same "
                "instance_id to finish it."
            ),
            # Keep the outcome next to vms[] and files_to_move[] when the
            # answer is reduced to a link to the stored result.
            "result_scalars": {
                "keys": [
                    "status",
                    "reason",
                    "instance_id",
                    "config_id",
                    "reset_router",
                    "move_state_files",
                    "resumed",
                ]
            },
        },
    ),
)
