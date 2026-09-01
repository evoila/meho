# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`MsadConnector`.

The connector manages **Active Directory** — domain / forest / DC facts, and
day-2 reads + guarded writes over users, groups, computers, and organizational
units — through the ``ActiveDirectory`` PowerShell module (Windows PowerShell
5.1) run on a **domain controller** over SSH, routed through the shared
PowerShell-over-SSH transport
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` (hoisted by #3260).
Structurally it is a copy of the ``winsrv`` estate mold (#3261): identity canary
+ per-domain op groups on the ``SshConnector`` base, the ``{rows, total}``
JSONFlux envelope for list reads, and the ``rke2`` approval-parked-write mold
for its destructive ops.

Op surface (27 ops across five groups; safety tier per the Initiative #3259
satellite table — reads ``safe``, recoverable writes ``caution``, destructive
``dangerous`` + ``requires_approval``):

* ``msad.about``                  [safe]      identity canary (Get-ADDomain minimal).
* ``msad.domain.info``            [safe]      Get-ADDomain (FSMO PDC/RID/Infra, mode).
* ``msad.domain.forest``          [safe]      Get-ADForest (FSMO schema/naming, mode).
* ``msad.domain.controllers``     [safe]      Get-ADDomainController -Filter * (list).
* ``msad.domain.replication``     [safe]      Get-ADReplicationPartnerMetadata (list).
* ``msad.user.list``              [safe]      Get-ADUser -Filter * (list).
* ``msad.user.get``               [safe]      Get-ADUser -Identity.
* ``msad.user.search``            [safe]      Get-ADUser -Filter {name/sam -like} (list).
* ``msad.user.create``            [caution]   New-ADUser (created disabled, no password).
* ``msad.user.set``               [caution]   Set-ADUser (attributes; never a password).
* ``msad.user.enable``            [caution]   Enable-ADAccount.
* ``msad.user.disable``           [caution]   Disable-ADAccount.
* ``msad.user.delete``            [dangerous+approval]  Remove-ADUser.
* ``msad.group.list``             [safe]      Get-ADGroup -Filter * (list).
* ``msad.group.get``              [safe]      Get-ADGroup -Identity.
* ``msad.group.members``          [safe]      Get-ADGroupMember (list).
* ``msad.group.add-member``       [caution]   Add-ADGroupMember.
* ``msad.group.remove-member``    [caution]   Remove-ADGroupMember.
* ``msad.group.delete``           [dangerous+approval]  Remove-ADGroup.
* ``msad.computer.list``          [safe]      Get-ADComputer -Filter * (list).
* ``msad.computer.get``           [safe]      Get-ADComputer -Identity.
* ``msad.computer.join-prestage`` [caution]   New-ADComputer.
* ``msad.computer.unjoin``        [caution]   Disable-ADAccount (recoverable).
* ``msad.computer.delete``        [dangerous+approval]  Remove-ADComputer.
* ``msad.ou.list``                [safe]      Get-ADOrganizationalUnit -Filter * (list).
* ``msad.ou.create``              [caution]   New-ADOrganizationalUnit.
* ``msad.ou.move``                [caution]   Move-ADObject.

**Password-reset is deferred, not shipped** (in-task decision; see
``docs/codebase/connectors-msad.md``): a plaintext AD password cannot ride the
``-EncodedCommand`` argv (the shared transport's secret-hygiene contract), so —
mirroring winsrv's passwordless-create — ``user.create`` creates a **disabled,
passwordless** account and no op carries a secret-value parameter. Provisioning
/ resetting a password is a Vault-brokered follow-up (the rke2 ``token.rotate``
mold).

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.winsrv.ops`; the composition pattern
(``_msad_ops()`` importing per-domain op tuples) mirrors it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from meho_backplane.connectors._shared.pwsh import pwsh_run

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.msad.connector import MsadConnector

__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "MSAD_OPS",
    "MSAD_WHEN_TO_USE_BY_GROUP",
    "SSH_TRANSPORT_NOTE",
    "MsadOp",
    "ad_list_read",
    "normalise_json_rows",
    "validate_limit",
]


#: Default cap on rows returned by a ``-Filter *`` list / search read. A real
#: AD domain can hold tens of thousands of objects; an unbounded pull would
#: blow the JSON payload and the pwsh timeout. Operators override per call via
#: the ``limit`` param; the value maps to the cmdlet's ``-ResultSetSize``.
DEFAULT_RESULT_LIMIT: int = 500


#: Curated ``when_to_use`` strings per group key, consumed by
#: :meth:`MsadConnector.register_operations` (the winsrv / rke2 precedent — the
#: blurbs live with the op metadata, not the transport class). The registration
#: walk fails closed with a :class:`ValueError` if a ``group_key`` lacks a
#: curated entry here.
MSAD_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "domain": (
        "Use for domain / forest topology facts before any object drill-in: "
        "identity (``msad.about``), the full domain projection with FSMO role "
        "holders (``msad.domain.info`` — PDC emulator / RID / infrastructure "
        "master), the forest projection (``msad.domain.forest`` — schema / "
        "domain-naming master, functional level), the DC inventory "
        "(``msad.domain.controllers``), and the inbound replication summary "
        "(``msad.domain.replication``). All read-only. Call ``msad.about`` "
        "first to confirm the target is a reachable domain controller."
    ),
    "users": (
        "Use to inventory and manage AD user accounts: list / get / search "
        "(read-only, ``safe``); create / set-attributes / enable / disable "
        "(``caution`` — recoverable); delete (``dangerous`` + "
        "``requires_approval`` — parks for a human). ``create`` makes a "
        "**disabled, passwordless** account — a plaintext password cannot ride "
        "the pwsh transport, so provision it out of band (password-reset is a "
        "Vault-brokered follow-up)."
    ),
    "groups": (
        "Use to inventory and manage AD groups and their membership: list / "
        "get / members (read-only, ``safe``); add-member / remove-member "
        "(``caution`` — recoverable); delete the group (``dangerous`` + "
        "``requires_approval``). The right group to grant a service account "
        "into a role group (e.g. add ``svc-sql`` to a SQL admins group)."
    ),
    "computers": (
        "Use to inventory and manage AD computer accounts: list / get "
        "(read-only, ``safe``); prestage a computer account so a machine can "
        "join the domain (``join-prestage``) and disable a computer account so "
        "it can no longer authenticate while the object is retained "
        "(``unjoin``) — both ``caution`` / recoverable; delete the account "
        "(``dangerous`` + ``requires_approval`` — irreversible)."
    ),
    "ou": (
        "Use to inventory and shape the OU tree: list OUs (read-only, "
        "``safe``); create a new OU (``create``) and move an object / OU to a "
        "new parent (``move``) — both ``caution``. Deleting an OU is out of "
        "scope for this connector cut."
    ),
}


#: Canonical SSH-only / pwsh transport reminder copied into every op's
#: ``llm_instructions`` (the winsrv ``SSH_TRANSPORT_NOTE`` precedent) so an LLM
#: does not compose against a non-existent AD REST surface.
SSH_TRANSPORT_NOTE: str = (
    "Active Directory exposes no unified REST API for these operations; the "
    "underlying transport is PowerShell-over-SSH (powershell -EncodedCommand "
    "routed through asyncssh) driving the Windows PowerShell 5.1 "
    "ActiveDirectory module cmdlets on a domain controller."
)


@dataclass(frozen=True)
class MsadOp:
    """Metadata for one msad op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can splat
    the dataclass into the helper. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.msad.connector.MsadConnector` exposing
    the async handler; the connector resolves the bound method against itself
    at registration time (identical shape to
    :class:`~meho_backplane.connectors.winsrv.ops.WinsrvOp`).
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def normalise_json_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a ``ConvertTo-Json`` payload into a list of row dicts.

    ``ConvertTo-Json`` renders a **single**-element result as a flat object and
    a **multi**-element result as a JSON array; a zero-element result renders as
    ``null`` (an empty stdout is caught upstream by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). This collapses
    all three shapes to a ``list[dict]`` — the winsrv ``normalise_json_rows``
    shape, shared across every list op so the ``{rows, total}`` envelope the
    JSONFlux reducer keys on is built identically.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def validate_limit(raw: Any, default: int = DEFAULT_RESULT_LIMIT) -> int:
    """Return a validated positive ``int`` result-set cap (default when absent).

    A non-int / bool / non-positive value raises :class:`ValueError` before any
    interpolation, so the ``-ResultSetSize`` operand is always a safe integer.
    """
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(f"limit must be a positive integer; got {raw!r}")
    return raw


async def ad_list_read(
    connector: MsadConnector,
    target: Any,
    *,
    pipeline: str,
    operator: Operator | None,
    prelude: str = "",
) -> dict[str, Any]:
    """Run a read *pipeline* under ``'Stop'`` and return the ``{rows, total}`` envelope.

    *pipeline* is the ``Get-AD… | Select-Object …`` expression whose output is
    wrapped in ``@(…)`` (so a single object still counts as one row and an
    empty result stays JSON-shaped — never an empty-stdout
    :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`). *prelude* is
    any statement(s) that must run first (e.g. a ``$q = '…'`` assignment for a
    search); it is emitted verbatim before the pipeline. Running under
    ``$ErrorActionPreference = 'Stop'`` turns a cmdlet error (bad identity,
    directory unreachable) into a real ``PwshRunError`` rather than a
    false-empty read.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        + prelude
        + f"$x = @({pipeline}); "
        + "ConvertTo-Json -Depth 4 -InputObject @{ rows = $x; total = $x.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


#: The identity canary op. ``msad.about`` is the operator-facing wrapper around
#: :meth:`MsadConnector.fingerprint` — same vendor / product / domain payload,
#: surfaced through the typed-op dispatcher so callers see the standard
#: :class:`OperationResult` envelope. Mirrors ``winsrv.about``.
_MSAD_ABOUT_OP = MsadOp(
    op_id="msad.about",
    handler_attr="about",
    summary="Return the AD domain identity (DNS root / NetBIOS / forest / DC) behind the target.",
    description=(
        "Connects to the domain controller over SSH and runs a single "
        "``powershell -EncodedCommand`` script that reads ``Get-ADDomain`` "
        "(DNS root, NetBIOS name, domain mode, forest, PDC emulator) plus the "
        "ActiveDirectory module version. Returns a flat dict with the vendor "
        "(``microsoft``), product (``active-directory``), the domain "
        "functional level as ``version``, and the domain identity. Use to "
        "confirm the target is a reachable domain controller before issuing "
        "higher-level domain / user / group / computer / ou ops; no params; "
        "safe on any healthy DC."
    ),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "dns_root": {"type": ["string", "null"]},
            "netbios_name": {"type": ["string", "null"]},
            "forest": {"type": ["string", "null"]},
            "pdc_emulator": {"type": ["string", "null"]},
            "ad_module_version": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="domain",
    tags=("read-only", "identity", "active-directory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the AD domain behind a "
            "target — its DNS root / NetBIOS name / forest / functional level "
            "— or to confirm the target is a reachable domain controller "
            "before issuing higher-level ops. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the domain functional level (e.g. "
            "``Windows2016Domain``), ``dns_root`` the domain DNS name (e.g. "
            "``c1sql.lab``), ``forest`` the forest root, ``pdc_emulator`` the "
            "PDC-emulator DC host name."
        ),
    },
)


def _msad_ops() -> tuple[MsadOp, ...]:
    """Return the merged registration tuple (identity canary + per-group tuples).

    Implemented as a function call (not a module-level literal-and-splat) so the
    import order stays linear: ``ops.py`` defines :class:`MsadOp` +
    ``_MSAD_ABOUT_OP`` + the shared helpers, then imports the per-domain op
    tuples from their modules. Mirrors
    :func:`meho_backplane.connectors.winsrv.ops._winsrv_ops`.
    """
    from meho_backplane.connectors.msad.ops_computers import COMPUTER_OPS
    from meho_backplane.connectors.msad.ops_domain import DOMAIN_OPS
    from meho_backplane.connectors.msad.ops_groups import GROUP_OPS
    from meho_backplane.connectors.msad.ops_ou import OU_OPS
    from meho_backplane.connectors.msad.ops_users import USER_OPS

    return (
        _MSAD_ABOUT_OP,
        *DOMAIN_OPS,
        *USER_OPS,
        *GROUP_OPS,
        *COMPUTER_OPS,
        *OU_OPS,
    )


#: The ops :class:`MsadConnector` registers at lifespan startup.
MSAD_OPS: tuple[MsadOp, ...] = _msad_ops()
