# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host runtime-state read verbs for :class:`LinuxSshConnector` (#3360).

Three ``safe`` scalar reads of live host state:

* ``linux.service.status`` (group ``service``) -- ``systemctl is-active`` /
  ``is-enabled`` / sub-state for one operator-named unit. Returns a flat
  dict ``{unit, active, enabled, sub_state}``. Raises
  :class:`LinuxServiceStatusProbeError` when ``systemctl`` is absent (a
  reachable non-systemd host) so an infrastructure gap is never served as
  a service-state verdict (the #986 discipline).
* ``linux.sysctl.read`` (group ``system``) -- ``sysctl -n <key>`` for one
  operator-named kernel parameter. Returns ``{key, value}`` (``value`` is
  ``null`` for an unknown key).
* ``linux.dns.lookup`` (group ``network``) -- ``getent ahosts <name>`` for
  one operator-named host name, through the host's own resolver. Returns
  ``{name, found, addresses, exit_code}``; a name the resolver cannot
  find is ``found=false``, not an error. Raises
  :class:`LinuxDnsLookupProbeError` when ``getent`` itself fails (absent
  binary, bad invocation) so an infrastructure gap is never served as a
  "not found" verdict.

All three re-validate the operator value against a strict charset *before*
constructing the command (fail-closed; the schema ``pattern`` is
advisory, the handler re-check authoritative -- the proxmox
method-allow-list mold) and quote it into a fixed command.
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
    "DNS_LOOKUP_TIMEOUT_S",
    "HOST_NAME_MAX_LENGTH",
    "HOST_NAME_PATTERN",
    "HOST_OPS",
    "SYSCTL_KEY_PATTERN",
    "UNIT_NAME_PATTERN",
    "LinuxDnsLookupProbeError",
    "LinuxServiceStatusProbeError",
    "build_dns_lookup_command",
    "build_service_status_command",
    "build_sysctl_read_command",
    "linux_dns_lookup",
    "linux_service_status",
    "linux_sysctl_read",
    "parse_dns_lookup_output",
    "parse_service_status_output",
    "parse_sysctl_read_output",
    "validate_host_name",
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

#: Host-name charset for ``linux.dns.lookup``: letters, digits, ``-`` and
#: ``.``, starting with a letter or digit (so a leading ``-`` can never be
#: read as a ``getent`` option), at most 253 characters (the DNS name
#: length limit). No shell metacharacter and no quote character is
#: admitted, so a name passing this cannot break out of the single quotes
#: the command builder wraps it in.
HOST_NAME_MAX_LENGTH: int = 253
HOST_NAME_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$"
_HOST_NAME_RE: re.Pattern[str] = re.compile(HOST_NAME_PATTERN)

#: Wall-clock cap for one ``getent ahosts`` round-trip. The host's own
#: resolver applies its own per-query timeouts and retries underneath; this
#: bound keeps a hung resolver from holding the SSH command open.
DNS_LOOKUP_TIMEOUT_S: float = 15.0

#: ``getent`` exit code for "the key was not found in the database" -- the
#: one non-zero exit that is a lookup verdict (``found=false``), not a
#: failure of the probe itself.
_GETENT_NOT_FOUND_EXIT: int = 2


class LinuxServiceStatusProbeError(RuntimeError):
    """The service-status probe could not run (no ``systemctl`` on the host).

    Distinct from any service-state verdict: the dispatcher maps it to a
    ``connector_error`` result (the #986 discipline) so a reachable
    non-systemd host is never served a fabricated ``inactive`` answer.
    """


class LinuxDnsLookupProbeError(RuntimeError):
    """The DNS lookup probe could not run (``getent`` absent or misused).

    Raised for any ``getent`` exit other than ``0`` (found) and ``2`` (not
    found), so a missing binary (exit ``127``) or a bad invocation (exit
    ``1``) is reported as a connector error, never as ``found=false``.
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


def validate_host_name(name: Any) -> str:
    """Return *name* if it matches the host-name charset, else raise.

    Fail-closed re-validation ahead of command construction. Refuses
    anything that is not a string, is empty, starts with ``-`` or ``.``,
    is longer than :data:`HOST_NAME_MAX_LENGTH`, or carries any character
    outside letters / digits / ``-`` / ``.``.
    """
    if not isinstance(name, str) or not _HOST_NAME_RE.fullmatch(name):
        raise ValueError(f"name {name!r} is not a valid host name")
    return name


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


def build_dns_lookup_command(name: str) -> str:
    """Build the fixed ``getent ahosts '<name>'`` command.

    Re-validates *name* itself, so an unvalidated value can never reach
    the quoting step. The validated charset admits no quote character and
    no shell metacharacter, so wrapping the name in single quotes is exact
    and complete: the command is always literally ``getent ahosts '<name>'``.
    ``getent`` resolves through the host's own name-service switch (on
    Ubuntu that is ``systemd-resolved``), so the answer is what programs
    on that host see.
    """
    return f"getent ahosts '{validate_host_name(name)}'"


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


def parse_dns_lookup_output(name: str, stdout: str, exit_status: int | None) -> dict[str, Any]:
    """Parse ``getent ahosts`` output into ``{name, found, addresses, exit_code}``.

    ``getent ahosts`` prints one line per socket address the resolver
    returned: ``<address> <socket type> [<canonical name>]`` -- typically
    three lines (``STREAM`` / ``DGRAM`` / ``RAW``) per distinct address.
    The first token of every non-empty line is collected, de-duplicated
    in first-seen order. Exit ``2`` is the resolver's "not found" verdict
    and yields ``found=false`` with no addresses. Any other non-zero exit
    (``127`` when ``getent`` is absent, ``1`` for a bad invocation) raises
    :class:`LinuxDnsLookupProbeError` -- a probe failure, not a lookup
    verdict.
    """
    exit_code = 0 if exit_status is None else exit_status
    if exit_code == _GETENT_NOT_FOUND_EXIT:
        return {"name": name, "found": False, "addresses": [], "exit_code": exit_code}
    if exit_code != 0:
        raise LinuxDnsLookupProbeError(
            f"getent ahosts exited {exit_code} for {name!r}; the lookup could not run"
        )
    addresses: list[str] = []
    for line in stdout.splitlines():
        parts = line.split()
        if parts and parts[0] not in addresses:
            addresses.append(parts[0])
    return {
        "name": name,
        "found": bool(addresses),
        "addresses": addresses,
        "exit_code": exit_code,
    }


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


async def linux_dns_lookup(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.dns.lookup`` -- resolve one name on the host."""
    name = validate_host_name(params["name"])
    cmd = build_dns_lookup_command(name)
    proc = await connector._run_command(
        target, cmd, operator=operator, timeout=DNS_LOOKUP_TIMEOUT_S
    )
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    return parse_dns_lookup_output(name, stdout, getattr(proc, "exit_status", 0))


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


_DNS_LOOKUP_OP = LinuxOp(
    op_id="linux.dns.lookup",
    handler_attr="dns_lookup",
    summary="Look up one host name through the host's own resolver and return its addresses.",
    description=(
        "Runs getent ahosts <name> on the host, so the answer is what programs "
        "on that host see. Returns {name, found, addresses, exit_code}. A name "
        "the resolver cannot find gives found=false, not an error. Read-only; "
        "the name is checked against a strict pattern before any command is built."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": HOST_NAME_MAX_LENGTH,
                "pattern": HOST_NAME_PATTERN,
                "description": (
                    "Host name to look up, e.g. registry.example.com. Letters, "
                    "digits, '-' and '.' only; must start with a letter or digit."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": "registry.example.com"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name that was looked up."},
            "found": {
                "type": "boolean",
                "description": "True when the resolver returned at least one address.",
            },
            "addresses": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IPv4 and IPv6 addresses the resolver returned, in order.",
            },
            "exit_code": {
                "type": "integer",
                "description": "getent exit code: 0 found, 2 not found.",
            },
        },
        "required": ["name", "found", "addresses", "exit_code"],
        "additionalProperties": True,
    },
    group_key="network",
    tags=("read-only", "dns", "network", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to prove that a Linux host can resolve a name through its own "
            "resolver -- for example after a resolver service was repaired, or "
            "when pods or programs on the host cannot reach a registry or mirror. "
            "Safe, so a sensor can run it on a schedule. For a lookup from MEHO's "
            "own side, use net.dns_lookup instead. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {
            "name": "One host name. No URL, no port, no leading '-'.",
        },
        "output_shape": (
            "{name, found, addresses: [..], exit_code}. found=false with an empty "
            "addresses list means the host's resolver could not find the name; "
            "that is a normal answer, not an error."
        ),
        "next_step": (
            "If found is false, call linux.service.status for systemd-resolved and "
            "linux.file.read on /run/systemd/resolve/resolv.conf to see which DNS "
            "servers the host uses. If found is true, the host's DNS works."
        ),
    },
)


#: The host runtime-state read tier composed onto ``LINUX_OPS``.
HOST_OPS: tuple[LinuxOp, ...] = (_SERVICE_STATUS_OP, _SYSCTL_READ_OP, _DNS_LOOKUP_OP)
