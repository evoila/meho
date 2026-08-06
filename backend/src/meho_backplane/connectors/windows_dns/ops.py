# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`WindowsDnsConnector`.

The connector manages Windows AD-DNS records over SSH → PowerShell
(``pwsh`` running the ``DnsServer`` module). Structurally it mirrors the
bind9 connector's op surface (identity + zone + record groups) but swaps
the BIND9 CLI (``named -v`` / ``dig`` / ``named-checkconf``) for the
Windows ``DnsServer`` cmdlets, routed through
:func:`~meho_backplane.connectors.windows_dns._pwsh.pwsh_run` (the
PowerShell-over-SSH transport the Holodeck connector established).

Op surface (mirrors bind9's read + record-write groups):

* ``windns.about``        [safe]    — identity canary: hostname +
  ``DnsServer`` module presence / version.
* ``windns.zone.list``    [safe]    — ``Get-DnsServerZone``.
* ``windns.record.get``   [safe]    — ``Get-DnsServerResourceRecord``.
* ``windns.record.add``   [caution] — ``Add-DnsServerResourceRecordA`` /
  ``Add-DnsServerResourceRecordCName``.
* ``windns.record.remove``[caution] — ``Remove-DnsServerResourceRecord``.

The safety-level assignment mirrors bind9 exactly: reads are ``safe``,
record mutations are ``caution`` (a DNS change is global — no per-caller
scoping — but recoverable; the production-path gate is G7/G10 policy
territory keyed on this value). ``requires_approval`` is ``False`` on
every op, matching bind9's record ops.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.bind9.ops` and
:mod:`~meho_backplane.connectors.holodeck.ops` so the registration walk
reads identically across SSH-transport connectors. The composition
pattern (``_windows_dns_ops()`` function that imports per-module op
tuples) also mirrors bind9 ``ops.py``'s ``_bind9_ops()`` shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["WINDOWS_DNS_OPS", "WindowsDnsOp"]


#: Canonical SSH-only / pwsh transport reminder copied into every op's
#: ``llm_instructions``. Mirrors Holodeck's ``SSH_TRANSPORT_NOTE`` — the
#: agent-facing descriptions must call out the PowerShell-over-SSH
#: transport so an LLM doesn't compose against a non-existent REST
#: surface (the Windows DNS role has no record-management REST API).
SSH_TRANSPORT_NOTE: str = (
    "Windows AD-DNS has no record-management REST API; the underlying "
    "transport is PowerShell-over-SSH (powershell -EncodedCommand routed "
    "through asyncssh) driving the DnsServer module cmdlets."
)


@dataclass(frozen=True)
class WindowsDnsOp:
    """Metadata for one windows_dns op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod
    can splat the dataclass into the helper without per-op
    boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.windows_dns.connector.WindowsDnsConnector`
    that exposes the async handler; the connector resolves the bound
    method against itself at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    walk can recover the callable from the persisted
    ``module.ClassName.method`` dotted path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: The identity canary op. ``windns.about`` is the operator-facing
#: wrapper around :meth:`WindowsDnsConnector.fingerprint` — same vendor /
#: product / version / host payload, surfaced through the typed-op
#: dispatcher so callers see the standard :class:`OperationResult`
#: envelope instead of the raw :class:`FingerprintResult`. Mirrors the
#: :data:`~meho_backplane.connectors.bind9.ops._BIND9_ABOUT_OP` entry.
_WINDNS_ABOUT_OP = WindowsDnsOp(
    op_id="windns.about",
    handler_attr="about",
    summary="Return the Windows DNS host's hostname + DnsServer module presence/version.",
    description=(
        "Connects to the Windows host over SSH and runs a single "
        "``powershell -EncodedCommand`` script that reads the machine "
        "hostname and inspects the ``DnsServer`` PowerShell module "
        "(``Get-Module -ListAvailable DnsServer``). Returns a flat "
        "dict with the vendor (``microsoft``), product "
        "(``windows-dns``), the DnsServer module version string, a "
        "boolean ``dnsserver_module_present`` flag, and the host "
        "name. Use to confirm the host is reachable over SSH + PowerShell "
        "and that the DnsServer role tooling is installed before "
        "issuing higher-level zone / record ops; no params; safe to "
        "call on any healthy target."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "build": {"type": ["string", "null"]},
            "hostname": {"type": ["string", "null"]},
            "dnsserver_module_present": {"type": ["boolean", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="identity",
    tags=("read-only", "identity", "windows-dns"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the Windows DNS "
            "host behind a target before issuing higher-level zone / "
            "record ops, or when the agent needs to confirm the host "
            "is reachable via SSH + PowerShell and the DnsServer module is "
            "installed. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the DnsServer module "
            "version (e.g. ``2.0.0.0``) when readable, ``None`` "
            "otherwise. ``dnsserver_module_present`` is True when the "
            "DnsServer module is installed. ``hostname`` carries the "
            "machine name."
        ),
    },
)


def _windows_dns_ops() -> tuple[WindowsDnsOp, ...]:
    """Return the merged registration tuple.

    Composition: ``windns.about`` (identity canary) + ``ZONE_OPS``
    (``windns.zone.list``) + ``RECORD_OPS`` (``windns.record.get`` +
    ``windns.record.add`` + ``windns.record.remove``). Five ops total.

    Implemented as a function call rather than a literal-and-splat at
    module level so the import order stays linear: ``ops.py`` defines
    :class:`WindowsDnsOp` + ``_WINDNS_ABOUT_OP``, then imports the
    per-area op tuples from their modules (each of which only depends
    on :class:`WindowsDnsOp` plus its own helpers). Mirrors
    :func:`meho_backplane.connectors.bind9.ops._bind9_ops`.
    """
    from meho_backplane.connectors.windows_dns.ops_record import RECORD_OPS
    from meho_backplane.connectors.windows_dns.ops_zone import ZONE_OPS

    return (_WINDNS_ABOUT_OP, *ZONE_OPS, *RECORD_OPS)


#: The ops :class:`WindowsDnsConnector` registers at lifespan startup —
#: identity canary + zone read + the three record ops (get / add /
#: remove). The shape of each follow-on op is "import a new module-level
#: tuple and splat it into :data:`WINDOWS_DNS_OPS` via
#: :func:`_windows_dns_ops`" — the registration walk in
#: :meth:`WindowsDnsConnector.register_operations` does not need to
#: change.
WINDOWS_DNS_OPS: tuple[WindowsDnsOp, ...] = _windows_dns_ops()
