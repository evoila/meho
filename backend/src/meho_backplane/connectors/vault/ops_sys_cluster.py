# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault ``sys`` cluster-wide node reads — typed handlers, schemas, LLM hints.

Op-ids, both under the ``sys`` operation group and registered from
:func:`meho_backplane.connectors.vault.ops_sys.register_vault_sys_typed_operations`:

* ``vault.sys.ha_status`` — every node of the cluster as the active
  node sees it (``GET /v1/sys/ha-status``).
* ``vault.sys.raft.autopilot_state`` — every raft server with its
  health, plus the cluster's failure tolerance
  (``GET /v1/sys/storage/raft/autopilot/state``).

Why these two exist: ``vault.sys.health`` and ``vault.sys.seal_status``
answer for the **one** node behind the target's address. In a cluster
with integrated storage (raft) that address reaches the active node, so
both say ``sealed: false`` while a standby node that came back sealed
after a restart is invisible. These reads list every node, so a sealed
or unhealthy standby shows up.

Both are ``safety_level='safe'`` / ``requires_approval=False`` and
return no secret content. Their op-ids end in ``.ha_status`` /
``.autopilot_state``, which are in the broadcast classifier's
read-suffix tuple so they broadcast as ``read`` like the sibling
``.health`` / ``.seal_status`` diagnostics.

hvac 2.4.0 has no helper for either endpoint, so the handlers call the
client's public ``adapter.get`` with the API path. That keeps hvac's
error mapping: a 403 raises :class:`hvac.exceptions.Forbidden`, which
the dispatcher turns into its normal Vault permission error. The
operator JWT is forwarded via
:func:`~meho_backplane.auth.vault.vault_client_for_operator` like the
other authenticated ``sys`` reads, and the blocking call is offloaded
with :func:`asyncio.to_thread`.

Vault policy: both paths need the ``read`` capability and neither is
sudo-protected. The autopilot endpoint must be called in the root
namespace.
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator

__all__ = [
    "VAULT_SYS_CLUSTER_PARAMETER_SCHEMA",
    "VAULT_SYS_HA_STATUS_LLM_INSTRUCTIONS",
    "VAULT_SYS_HA_STATUS_RESPONSE_SCHEMA",
    "VAULT_SYS_RAFT_AUTOPILOT_STATE_LLM_INSTRUCTIONS",
    "VAULT_SYS_RAFT_AUTOPILOT_STATE_RESPONSE_SCHEMA",
    "vault_sys_ha_status",
    "vault_sys_raft_autopilot_state",
]

HA_STATUS_API_PATH = "/v1/sys/ha-status"
RAFT_AUTOPILOT_STATE_API_PATH = "/v1/sys/storage/raft/autopilot/state"

#: Neither read takes a parameter. ``additionalProperties=False`` rejects a
#: stray key with a clear validation error; the empty example is the one
#: valid call shape.
VAULT_SYS_CLUSTER_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

#: Node fields copied from each ``ha-status`` entry. The Enterprise-only
#: extras (``upgrade_version``, ``redundancy_zone``, ...) are dropped so
#: the row shape is the same on every edition.
_HA_NODE_FIELDS = (
    "hostname",
    "api_address",
    "cluster_address",
    "active_node",
    "last_echo",
    "version",
    "clock_skew_ms",
    "echo_duration_ms",
)

#: Server fields copied from each autopilot ``servers`` entry.
_RAFT_SERVER_FIELDS = (
    "id",
    "name",
    "address",
    "status",
    "node_status",
    "node_type",
    "healthy",
    "last_contact",
    "last_index",
    "last_term",
    "stable_since",
    "version",
)


VAULT_SYS_HA_STATUS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "description": (
                "One row per cluster node the active node has heard from "
                "since it became active, including itself."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "hostname": {"type": ["string", "null"]},
                    "api_address": {"type": ["string", "null"]},
                    "cluster_address": {"type": ["string", "null"]},
                    "active_node": {
                        "type": ["boolean", "null"],
                        "description": "True for the node that serves requests.",
                    },
                    "last_echo": {
                        "type": ["string", "null"],
                        "description": (
                            "When the active node last heard from this node "
                            "(RFC 3339). Null for the active node itself."
                        ),
                    },
                    "version": {"type": ["string", "null"]},
                    "clock_skew_ms": {"type": ["integer", "null"]},
                    "echo_duration_ms": {"type": ["integer", "null"]},
                },
            },
        },
    },
    "required": ["nodes"],
}


VAULT_SYS_RAFT_AUTOPILOT_STATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "healthy": {
            "type": ["boolean", "null"],
            "description": "True when autopilot considers the whole cluster healthy.",
        },
        "failure_tolerance": {
            "type": ["integer", "null"],
            "description": (
                "How many nodes can fail, one after another, before the cluster stops."
            ),
        },
        "leader": {"type": ["string", "null"], "description": "Raft id of the leader."},
        "voters": {"type": "array", "items": {"type": "string"}},
        "servers": {
            "type": "array",
            "description": "One row per raft server, sorted by id.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "address": {"type": ["string", "null"]},
                    "status": {
                        "type": ["string", "null"],
                        "description": "'leader', 'voter' or 'non-voter'.",
                    },
                    "node_status": {"type": ["string", "null"]},
                    "node_type": {"type": ["string", "null"]},
                    "healthy": {"type": ["boolean", "null"]},
                    "last_contact": {
                        "type": ["string", "null"],
                        "description": "Time since the leader last heard from this server.",
                    },
                    "last_index": {"type": ["integer", "null"]},
                    "last_term": {"type": ["integer", "null"]},
                    "stable_since": {"type": ["string", "null"]},
                    "version": {"type": ["string", "null"]},
                },
            },
        },
    },
    "required": ["healthy", "failure_tolerance", "servers"],
}


VAULT_SYS_HA_STATUS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "List every node of a Vault cluster with its address, whether it "
        "is the active node, and when the active node last heard from it. "
        "Use when an operator asks 'is every Vault node up?' or after a "
        "node restart. vault.sys.health and vault.sys.seal_status answer "
        "for the one node behind the target's address only; this read "
        "shows all nodes. Read-only; no secret content."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'nodes': [{'hostname', 'api_address', 'cluster_address', "
        "'active_node', 'last_echo', 'version', 'clock_skew_ms', "
        "'echo_duration_ms'}, ...]}. A node missing from the list has not "
        "reported to the active node. On a 403: the normal Vault "
        "permission error (the meho Vault role needs 'read' on "
        "'sys/ha-status')."
    ),
    "next_step": (
        "If a node is missing or its last_echo is old, call "
        "vault.sys.raft.autopilot_state for its health and the cluster's "
        "failure tolerance. Then check the node's pod or host with the "
        "Kubernetes or SSH connector; a sealed node needs an unseal by an "
        "operator."
    ),
}


VAULT_SYS_RAFT_AUTOPILOT_STATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Check the health of every raft server in a Vault cluster with "
        "integrated storage, plus how many more nodes may fail before "
        "Vault stops (failure_tolerance). Use when a node restarted and "
        "may have come back sealed, or when an operator asks 'how much "
        "spare capacity does the Vault cluster have?'. vault.sys.health "
        "and vault.sys.seal_status answer for one node only; this read "
        "shows all. Only clusters with raft storage answer. Read-only; no "
        "secret content."
    ),
    "parameter_hints": {},
    "output_shape": (
        "{'healthy': <bool>, 'failure_tolerance': <int>, 'leader': <id>, "
        "'voters': [<id>, ...], 'servers': [{'id', 'name', 'address', "
        "'status', 'node_status', 'node_type', 'healthy', 'last_contact', "
        "'last_index', 'last_term', 'stable_since', 'version'}, ...]}. A "
        "server with healthy=false needs attention. On a 403: the normal "
        "Vault permission error (the meho Vault role needs 'read' on "
        "'sys/storage/raft/autopilot/state')."
    ),
    "next_step": (
        "For a server with healthy=false, check its pod or host with the "
        "Kubernetes or SSH connector and ask an operator to unseal it if it "
        "is sealed. Use vault.sys.ha_status for the nodes' API addresses. "
        "A failure_tolerance of 0 means the next failure stops Vault."
    ),
    # Keep the cluster verdict next to the reduced server list when the
    # result is turned into a stored result.
    "result_scalars": {"keys": ["healthy", "failure_tolerance", "leader"]},
}


def _unwrap_data(response: Any) -> dict[str, Any]:
    """Return the ``data`` object of a Vault envelope, or the body itself."""
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    return data if isinstance(data, dict) else {}


def _pick(entry: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Copy the named fields from one node/server entry, null when absent."""
    source = entry if isinstance(entry, dict) else {}
    return {field: source.get(field) for field in fields}


async def vault_sys_ha_status(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List every cluster node (``GET /v1/sys/ha-status``).

    Op-id: ``vault.sys.ha_status``. Safe / read-only.

    Returns ``{"nodes": [<row>, ...]}`` with one row per node. Vault
    returns the list under ``data.Nodes`` (capital N); the handler also
    accepts a lower-case key so a future rename does not break it.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    hvac.exceptions.Forbidden
        The operator's Vault policy lacks ``read`` on ``sys/ha-status``.
        The dispatcher turns it into the Vault permission error.
    Exception
        Any other error hvac raises from the read.
    """
    _ = params  # schema-validated empty object; no inputs to extract.
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.adapter.get, HA_STATUS_API_PATH)
    data = _unwrap_data(response)
    raw_nodes = data.get("Nodes", data.get("nodes"))
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    return {"nodes": [_pick(node, _HA_NODE_FIELDS) for node in nodes]}


async def vault_sys_raft_autopilot_state(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read raft autopilot state (``GET /v1/sys/storage/raft/autopilot/state``).

    Op-id: ``vault.sys.raft.autopilot_state``. Safe / read-only.

    Vault returns ``servers`` as a map keyed by raft id. The handler
    turns it into a list of rows sorted by id so the answer is
    set-shaped and goes through the usual result reduction.

    Raises
    ------
    meho_backplane.auth.vault.VaultClientError
        Login-phase failure. Wrapped into ``connector_error``.
    hvac.exceptions.Forbidden
        The operator's Vault policy lacks ``read`` on
        ``sys/storage/raft/autopilot/state``. The dispatcher turns it
        into the Vault permission error.
    Exception
        Any other error hvac raises from the read (for example a Vault
        without raft storage).
    """
    _ = params
    async with _auth_vault.vault_client_for_operator(operator) as client:
        response = await asyncio.to_thread(client.adapter.get, RAFT_AUTOPILOT_STATE_API_PATH)
    data = _unwrap_data(response)
    raw_servers = data.get("servers")
    if isinstance(raw_servers, dict):
        entries = [
            {"id": server_id, **entry} if isinstance(entry, dict) else {"id": server_id}
            for server_id, entry in raw_servers.items()
        ]
    elif isinstance(raw_servers, list):
        entries = raw_servers
    else:
        entries = []
    servers = sorted(
        (_pick(entry, _RAFT_SERVER_FIELDS) for entry in entries),
        key=lambda row: str(row.get("id") or ""),
    )
    voters = data.get("voters")
    return {
        "healthy": data.get("healthy"),
        "failure_tolerance": data.get("failure_tolerance"),
        "leader": data.get("leader"),
        "voters": list(voters) if isinstance(voters, list) else [],
        "servers": servers,
    }
