# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""wsfc witness ops — ``get`` (safe) + ``set`` (caution).

The quorum witness is driven by ``Get-ClusterQuorum`` (read) and
``Set-ClusterQuorum`` (reconfigure) over the shared PowerShell-over-SSH
transport.

* ``wsfc.witness.get`` resolves the witness *resource* from
  ``Get-ClusterQuorum.QuorumResource`` and reads its online state via
  ``Get-ClusterResource`` — the dynamic signal a Sensor pins (``$.online``),
  load-bearing on a 2-node cluster where a downed witness is one node failure
  from quorum loss. A node-majority cluster has no witness, so ``online`` reads
  ``false`` there — pin this recipe only where a witness is expected.
* ``wsfc.witness.set`` reconfigures the witness (``caution``): a **disk**
  witness (``Set-ClusterQuorum -DiskWitness <resource>``), a **file-share**
  witness (``-FileShareWitness <UNC path>``), or **node majority / no witness**
  (``-NoWitness``).

No cloud witness (deliberate)
-----------------------------

``Set-ClusterQuorum -CloudWitness`` needs ``-AccessKey`` (an Azure storage
account key). That secret would land in the ``-EncodedCommand`` script — a
violation of the shared transport's secret-hygiene contract (see
:mod:`~meho_backplane.connectors._shared.pwsh`, the same reason winsrv's
``iscsi.connect`` has no CHAP). A cloud witness is therefore out of scope for
this cut; a Vault-brokered flow (mint-and-stash, so no secret enters the
script) is a follow-up.

PowerShell injection safety
---------------------------

The operator-supplied disk-resource name / file-share path is interpolated only
inside a single-quoted PowerShell literal via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; ``witness_type``
is validated against a bounded enum before it selects a fixed cmdlet flag.

References
----------

* ``Get-ClusterQuorum`` / ``Set-ClusterQuorum`` / ``Get-ClusterResource``
  (FailoverClusters):
  https://learn.microsoft.com/en-us/powershell/module/failoverclusters/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.wsfc.ops import SSH_TRANSPORT_NOTE, WsfcOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.wsfc.connector import WsfcConnector

__all__ = [
    "WITNESS_OPS",
    "wsfc_witness_get",
    "wsfc_witness_set",
]

#: Resolve the witness resource from Get-ClusterQuorum and read its online
#: state. Constant script (no operator input). ``QuorumResource`` stringifies
#: to the resource name; ``Get-ClusterResource -Name`` then reads the state.
_WITNESS_GET_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$q = Get-ClusterQuorum; "
    '$wname = if ($q.QuorumResource) { "$($q.QuorumResource)" } else { $null }; '
    "$wstate = $null; $wtype = $null; $online = $false; "
    "if ($wname) { $r = Get-ClusterResource -Name $wname -ErrorAction SilentlyContinue; "
    'if ($r) { $wstate = "$($r.State)"; $wtype = "$($r.ResourceType)"; '
    "$online = (\"$($r.State)\" -eq 'Online') } }; "
    'ConvertTo-Json -Compress -InputObject @{ cluster = "$($q.Cluster)"; '
    'quorum_type = "$($q.QuorumType)"; witness_resource = $wname; '
    "witness_type = $wtype; witness_state = $wstate; online = $online }"
)

#: witness_type → the Set-ClusterQuorum flag it selects. ``disk`` /
#: ``file_share`` also consume an operator string (resource / path);
#: ``node_majority`` is a bare switch. Cloud witness is intentionally absent
#: (its access key cannot ride the transport — see the module docstring).
_WITNESS_TYPES: frozenset[str] = frozenset({"node_majority", "disk", "file_share"})


async def wsfc_witness_get(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.witness.get`` — the witness resource + its online state.

    Returns ``{cluster, quorum_type, witness_resource, witness_type,
    witness_state, online}``. ``online`` is the boolean a Sensor pins
    (``$.online``); it is ``false`` when the cluster has no witness
    (node-majority).
    """
    del params
    payload = await pwsh_run(connector, target, _WITNESS_GET_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


def _witness_clause(params: dict[str, Any]) -> str:
    """Return the validated ``Set-ClusterQuorum`` witness clause from *params*."""
    witness_type = params.get("witness_type")
    if witness_type not in _WITNESS_TYPES:
        raise ValueError(
            f"witness_type must be one of {sorted(_WITNESS_TYPES)}; got {witness_type!r}"
        )
    if witness_type == "node_majority":
        return "-NoWitness"
    if witness_type == "disk":
        resource = params.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise ValueError(
                "witness_type='disk' requires a non-empty 'resource' (disk resource name)"
            )
        return f"-DiskWitness {ps_single_quote(resource)}"
    # file_share
    path = params.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("witness_type='file_share' requires a non-empty 'path' (UNC share path)")
    return f"-FileShareWitness {ps_single_quote(path)}"


async def wsfc_witness_set(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.witness.set`` (caution) — reconfigure the quorum witness.

    ``witness_type`` selects the witness model: ``disk`` (needs ``resource``),
    ``file_share`` (needs ``path``), or ``node_majority`` (no witness). Reads
    back the resulting quorum model. No cloud witness (see the module
    docstring).
    """
    clause = _witness_clause(params)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Set-ClusterQuorum {clause} | Out-Null; "
        "$q = Get-ClusterQuorum; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        'quorum_type = "$($q.QuorumType)"; '
        'quorum_resource = if ($q.QuorumResource) { "$($q.QuorumResource)" } else { $null } }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {
        "action": "set",
        "witness_type": params["witness_type"],
        "quorum_type": data.get("quorum_type"),
        "quorum_resource": data.get("quorum_resource"),
        "op_class": "write",
    }


WITNESS_OPS: tuple[WsfcOp, ...] = (
    WsfcOp(
        op_id="wsfc.witness.get",
        handler_attr="wsfc_witness_get",
        summary="Read the quorum witness resource + its online state.",
        description=(
            "Resolves the witness resource from ``Get-ClusterQuorum`` and reads "
            "its online state via ``Get-ClusterResource``. Returns "
            "``{cluster, quorum_type, witness_resource, witness_type, "
            "witness_state, online}``. Read-only — the ``online`` boolean is "
            "the op a Sensor pins to watch witness reachability (load-bearing "
            "on a 2-node cluster). A node-majority cluster has no witness, so "
            "``online`` reads ``false`` there."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema={
            "type": "object",
            "properties": {
                "cluster": {"type": ["string", "null"]},
                "quorum_type": {"type": ["string", "null"]},
                "witness_resource": {"type": ["string", "null"]},
                "witness_type": {"type": ["string", "null"]},
                "witness_state": {"type": ["string", "null"]},
                "online": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        group_key="witness",
        tags=("read-only", "witness", "quorum"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to check the quorum witness — which resource is the "
                "witness and whether it is Online. Read-only; the sensor op "
                "for witness reachability. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'cluster', 'quorum_type', 'witness_resource', 'witness_type', "
                "'witness_state', 'online': <bool>}."
            ),
        },
    ),
    WsfcOp(
        op_id="wsfc.witness.set",
        handler_attr="wsfc_witness_set",
        summary="Reconfigure the quorum witness via ``Set-ClusterQuorum`` (caution).",
        description=(
            "Reconfigures the cluster quorum witness. ``witness_type`` selects "
            "the model: ``disk`` (``-DiskWitness <resource>``, needs "
            "``resource``), ``file_share`` (``-FileShareWitness <UNC path>``, "
            "needs ``path``), or ``node_majority`` (``-NoWitness``). No cloud "
            "witness — its access key cannot ride the pwsh transport (a "
            "Vault-brokered flow is a follow-up). safety_level=caution — a "
            "recoverable quorum reconfiguration."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "witness_type": {
                    "type": "string",
                    "enum": sorted(_WITNESS_TYPES),
                    "description": "The witness model: disk / file_share / node_majority.",
                },
                "resource": {
                    "type": "string",
                    "description": "Disk resource name (required for ``witness_type='disk'``).",
                },
                "path": {
                    "type": "string",
                    "description": "UNC share path (required for ``witness_type='file_share'``).",
                },
            },
            "required": ["witness_type"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "witness_type": {"type": "string"},
                "quorum_type": {"type": ["string", "null"]},
                "quorum_resource": {"type": ["string", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["action", "witness_type", "op_class"],
            "additionalProperties": True,
        },
        group_key="witness",
        tags=("write", "witness", "quorum"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Reconfigure the quorum witness (disk / file-share / node "
                "majority). Recoverable; safety_level=caution. No cloud "
                "witness in this cut. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "witness_type": "Required. disk / file_share / node_majority.",
                "resource": "Disk resource name (for witness_type=disk).",
                "path": "UNC share path (for witness_type=file_share).",
            },
            "output_shape": (
                "{'action': 'set', 'witness_type', 'quorum_type', "
                "'quorum_resource', 'op_class': 'write'}."
            ),
        },
    ),
)
