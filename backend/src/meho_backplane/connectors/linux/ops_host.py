# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host runtime-state read verbs for :class:`LinuxSshConnector` (#3360).

Two ``safe`` scalar reads of live host state:

* ``linux.service.status`` (group ``service``) -- ``systemctl is-active`` /
  ``is-enabled`` / sub-state for one operator-named unit. Returns a flat
  dict ``{unit, active, enabled, sub_state}``. Raises
  :class:`LinuxServiceStatusProbeError` when ``systemctl`` is absent (a
  reachable non-systemd host) so an infrastructure gap is never served as
  a service-state verdict (the #986 discipline).
* ``linux.sysctl.read`` (group ``system``) -- ``sysctl -n <key>`` for one
  operator-named kernel parameter. Returns ``{key, value}`` (``value`` is
  ``null`` for an unknown key).

Both re-validate the operator value against a strict charset *before*
constructing the command (fail-closed; the schema ``pattern`` is
advisory, the handler re-check authoritative -- the proxmox
method-allow-list mold) and ``shlex.quote`` it into a fixed command.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import SSH_TRANSPORT_NOTE, LinuxOp

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "HOST_OPS",
    "SYSCTL_KEY_PATTERN",
    "UNIT_NAME_PATTERN",
    "LinuxServiceStatusProbeError",
    "build_service_status_command",
    "build_sysctl_read_command",
    "linux_service_status",
    "linux_sysctl_read",
    "parse_service_status_output",
    "parse_sysctl_read_output",
    "validate_sysctl_key",
    "validate_unit_name",
]

#: systemd unit-name charset. Covers ``nginx``, ``sshd.service``,
#: ``getty@tty1.service`` (``@`` instance, ``.`` type suffix, ``-`` / ``:``
#: / ``_`` in template names). No shell metacharacters are admitted, so a
#: name passing this can carry no injection payload -- ``shlex.quote`` is
#: still applied defensively.
UNIT_NAME_PATTERN: str = r"^[A-Za-z0-9:._@-]+$"
_UNIT_NAME_RE: re.Pattern[str] = re.compile(UNIT_NAME_PATTERN)

#: sysctl key charset. Covers dotted (``net.ipv4.ip_forward``) and slashed
#: (``net/ipv4/ip_forward``) spellings; no shell metacharacters.
SYSCTL_KEY_PATTERN: str = r"^[A-Za-z0-9._/-]+$"
_SYSCTL_KEY_RE: re.Pattern[str] = re.compile(SYSCTL_KEY_PATTERN)

#: Exit code the service-status command uses when the host has no
#: ``systemctl``. Any non-zero exit is an infrastructure failure, not a
#: service-state verdict.
_NO_SYSTEMCTL_EXIT: int = 127


class LinuxServiceStatusProbeError(RuntimeError):
    """The service-status probe could not run (no ``systemctl`` on the host).

    Distinct from any service-state verdict: the dispatcher maps it to a
    ``connector_error`` result (the #986 discipline) so a reachable
    non-systemd host is never served a fabricated ``inactive`` answer.
    """


def validate_unit_name(unit: Any) -> str:
    """Return *unit* if it matches the unit-name charset, else raise.

    Fail-closed re-validation ahead of command construction: the schema
    ``pattern`` is advisory, this check is authoritative.
    """
    if not isinstance(unit, str) or not _UNIT_NAME_RE.fullmatch(unit):
        raise ValueError(f"unit {unit!r} is not a valid systemd unit name")
    return unit


def validate_sysctl_key(key: Any) -> str:
    """Return *key* if it matches the sysctl-key charset, else raise."""
    if not isinstance(key, str) or not _SYSCTL_KEY_RE.fullmatch(key):
        raise ValueError(f"key {key!r} is not a valid sysctl key")
    return key


def build_service_status_command(unit: str) -> str:
    """Build the fixed single-round-trip ``service.status`` command.

    Exits ``127`` up front when ``systemctl`` is absent (the non-systemd
    guard). Otherwise captures ``is-active`` / ``is-enabled`` / the
    ``SubState`` in ``$(...)`` so the overall command still exits 0 for an
    inactive or disabled unit (both sub-commands exit non-zero then). The
    unit name is ``shlex.quote``d.
    """
    quoted = shlex.quote(unit)
    return (
        f"command -v systemctl >/dev/null 2>&1 || exit {_NO_SYSTEMCTL_EXIT}; "
        f"printf 'active=%s\\n' \"$(systemctl is-active {quoted} 2>/dev/null)\"; "
        f"printf 'enabled=%s\\n' \"$(systemctl is-enabled {quoted} 2>/dev/null)\"; "
        f"printf 'sub=%s\\n' \"$(systemctl show -p SubState --value {quoted} 2>/dev/null)\""
    )


def build_sysctl_read_command(key: str) -> str:
    """Build the fixed ``sysctl -n <key>`` command for a validated key."""
    return f"sysctl -n {shlex.quote(key)} 2>/dev/null"


def _kv(stdout: str) -> dict[str, str]:
    """Parse ``key=value`` lines into a dict (last write wins)."""
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def parse_service_status_output(unit: str, stdout: str) -> dict[str, Any]:
    """Parse the ``service.status`` output into ``{unit, active, enabled, sub_state}``.

    An empty captured value (the sub-command printed nothing) resolves to
    ``None`` rather than an empty string, so a truly-missing field is
    distinguishable. ``systemctl is-active`` prints ``inactive`` /
    ``failed`` / ``active``; ``is-enabled`` prints ``enabled`` /
    ``disabled`` / ``static`` / ``masked``; these pass through verbatim.
    """
    fields = _kv(stdout)

    def _val(name: str) -> str | None:
        raw = fields.get(name)
        return raw if raw else None

    return {
        "unit": unit,
        "active": _val("active"),
        "enabled": _val("enabled"),
        "sub_state": _val("sub"),
    }


def parse_sysctl_read_output(key: str, stdout: str, exit_status: int | None) -> dict[str, Any]:
    """Parse the ``sysctl.read`` output into ``{key, value}``.

    A non-zero exit (unknown key, ``sysctl`` absent) yields ``value=None``.
    A successful read returns the raw value with surrounding whitespace
    stripped (kernel scalars have no meaningful trailing newline).
    """
    if exit_status not in (0, None):
        return {"key": key, "value": None}
    value = stdout.strip()
    return {"key": key, "value": value if value else None}


async def linux_service_status(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.service.status`` -- read one systemd unit's state."""
    unit = validate_unit_name(params["unit"])
    cmd = build_service_status_command(unit)
    proc = await connector._run_command(target, cmd, operator=operator)
    if getattr(proc, "exit_status", 0) == _NO_SYSTEMCTL_EXIT:
        raise LinuxServiceStatusProbeError(
            f"host has no systemctl; cannot read service state for {unit!r}"
        )
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    return parse_service_status_output(unit, stdout)


async def linux_sysctl_read(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.sysctl.read`` -- read one kernel parameter."""
    key = validate_sysctl_key(params["key"])
    cmd = build_sysctl_read_command(key)
    proc = await connector._run_command(target, cmd, operator=operator)
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    return parse_sysctl_read_output(key, stdout, getattr(proc, "exit_status", 0))


_SERVICE_STATUS_OP = LinuxOp(
    op_id="linux.service.status",
    handler_attr="service_status",
    summary="Report is-active / is-enabled / sub-state for one systemd unit.",
    description=(
        "Runs ``systemctl is-active`` / ``is-enabled`` plus a ``SubState`` "
        "read for a single named unit and returns ``{unit, active, enabled, "
        "sub_state}``. The unit name is charset-validated (fail-closed) and "
        "``shlex.quote``d. Read-only -- it never starts, stops, enables, or "
        "restarts the unit. On a reachable host with no ``systemctl`` it "
        "raises a connector error rather than fabricating an ``inactive`` "
        "verdict."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "minLength": 1,
                "pattern": UNIT_NAME_PATTERN,
                "description": "systemd unit name, e.g. ``nginx`` or ``sshd.service``.",
            },
        },
        "required": ["unit"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "unit": {"type": "string"},
            "active": {"type": ["string", "null"]},
            "enabled": {"type": ["string", "null"]},
            "sub_state": {"type": ["string", "null"]},
        },
        "required": ["unit"],
        "additionalProperties": True,
    },
    group_key="service",
    tags=("read-only", "service", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm a declared systemd unit (DNS, DHCP, NTP, a "
            "first-boot service) is actually active and enabled, not merely "
            "installed -- without changing it. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {"unit": "A single systemd unit name."},
        "output_shape": (
            "``{unit, active, enabled, sub_state}``; ``active`` is "
            "systemd's ``active`` / ``inactive`` / ``failed`` verdict."
        ),
    },
)


_SYSCTL_READ_OP = LinuxOp(
    op_id="linux.sysctl.read",
    handler_attr="sysctl_read",
    summary="Read the live value of one kernel parameter (sysctl).",
    description=(
        "Runs ``sysctl -n <key>`` for a single kernel parameter and returns "
        "``{key, value}``. The key is charset-validated (dotted or slashed "
        "spelling; fail-closed) and ``shlex.quote``d. An unknown key (or a "
        "host without ``sysctl``) returns ``value=null``. Read-only -- the "
        "kernel-parameter *write* verb is the approval-gated op."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "minLength": 1,
                "pattern": SYSCTL_KEY_PATTERN,
                "description": "Kernel parameter, e.g. ``net.ipv4.ip_forward``.",
            },
        },
        "required": ["key"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"type": ["string", "null"]},
        },
        "required": ["key"],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "sysctl", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm a kernel parameter the first-boot config was "
            "meant to set is actually live -- e.g. did "
            "``net.ipv4.ip_forward`` end up ``1`` on a router VM. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {"key": "A single sysctl key, dotted or slashed."},
        "output_shape": "``{key, value}``; ``value`` is ``null`` for an unknown key.",
    },
)


#: The host runtime-state read tier composed onto ``LINUX_OPS``.
HOST_OPS: tuple[LinuxOp, ...] = (_SERVICE_STATUS_OP, _SYSCTL_READ_OP)
