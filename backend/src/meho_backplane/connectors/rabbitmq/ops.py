# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`RabbitMqConnector` (#2233).

The bounded, **read-only** set of RabbitMQ Management HTTP API ops that
let an agent inspect a broker through ``call_operation`` without reaching
for ``rabbitmqctl`` or the management UI: cluster identity + health,
messaging topology, live connectivity, cross-site shovel / federation
topology, and the full broker definitions export. Every op is a single
``GET`` against the ``/api`` surface — :class:`RabbitMqConnector`'s method
gate guarantees no other verb reaches the wire.

Each op's ``tags`` carry the RabbitMQ **user tag** the broker user needs
to read that surface (the second load-bearing nuance beside redaction):

* ``monitoring`` — the whole-cluster observability surface: overview,
  nodes, topology (exchanges / queues / bindings / vhosts), live
  connections / channels / consumers, and shovel / federation *status*.
* ``policymaker`` — the runtime-parameter surface: dynamic shovel
  definitions, federation-upstream parameters, and policies.
* ``administrator`` — the full ``/api/definitions`` export (users, their
  ``password_hash``, every parameter and policy).

The dataclass + tuple shape mirrors the ArgoCD (#1391) and bind9 (#367)
connectors so the registration walk reads identically. Handler methods
live on :class:`~meho_backplane.connectors.rabbitmq.connector.RabbitMqConnector`
(each a thin ``GET`` via a shared read helper) so the persisted
``handler_ref`` round-trips through the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk.

Endpoint facts are pinned to the RabbitMQ Management HTTP API reference
(https://www.rabbitmq.com/docs/http-api-reference and
https://www.rabbitmq.com/docs/management): the collection endpoints
(``/api/queues``, ``/api/exchanges``, ``/api/bindings``, ``/api/nodes``,
…) return a bare JSON **array**; ``/api/overview`` and
``/api/definitions`` return an object. Vhost-scoped variants
(``/api/queues/{vhost}`` etc.) are addressed by the optional ``vhost``
param. Shovel status is served by the ``rabbitmq_shovel_management``
plugin at ``/api/shovels``; dynamic shovel definitions live as runtime
parameters at ``/api/parameters/shovel``; federation link status is
served by ``rabbitmq_federation_management`` at ``/api/federation-links``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "RABBITMQ_OPS",
    "RABBITMQ_REDACTED_OP_IDS",
    "RABBITMQ_WHEN_TO_USE_BY_GROUP",
    "RabbitMqOp",
]


@dataclass(frozen=True)
class RabbitMqOp:
    """Metadata for one rabbitmq op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper
    without per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.rabbitmq.connector.RabbitMqConnector`
    exposing the async handler; the registrar resolves the bound method at
    registration time so the dispatcher can recover the callable from the
    persisted ``module.ClassName.method`` path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str
    tags: tuple[str, ...]
    llm_instructions: dict[str, Any]
    safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    requires_approval: bool = False


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the
#: registrar looks each op's ``group_key`` up here.
RABBITMQ_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "rabbitmq-cluster": (
        "Use for RabbitMQ cluster identity and health: the whole-system "
        "overview (rabbitmq.overview — versions, message rates, object "
        "totals, listeners) and per-node state (rabbitmq.nodes — running "
        "flag, disc/ram type, memory/disk/fd/socket alarms). Start here to "
        "confirm the broker is reachable and healthy. Needs the RabbitMQ "
        "'monitoring' user tag."
    ),
    "rabbitmq-topology": (
        "Use for RabbitMQ messaging topology: exchanges "
        "(rabbitmq.exchanges), queues with depth/consumer counts "
        "(rabbitmq.queues), the exchange/queue binding graph "
        "(rabbitmq.bindings), and the virtual hosts they live in "
        "(rabbitmq.vhosts). The right group for 'what queues exist and how "
        "deep are they?' or 'how is this exchange wired?'. Pass 'vhost' to "
        "scope to one virtual host. Needs the 'monitoring' user tag for "
        "cluster-wide visibility."
    ),
    "rabbitmq-connectivity": (
        "Use for live RabbitMQ client connectivity: open connections "
        "(rabbitmq.connections), channels (rabbitmq.channels), and "
        "consumers (rabbitmq.consumers). The right group for 'who is "
        "connected?', 'which consumers are draining this queue?', or "
        "diagnosing a client that stopped consuming. Needs the "
        "'monitoring' user tag."
    ),
    "rabbitmq-federation": (
        "Use for cross-site RabbitMQ shovel and federation topology: "
        "dynamic shovel definitions (rabbitmq.shovels) and their live "
        "status (rabbitmq.shovel_status), federation link status "
        "(rabbitmq.federation_links), runtime parameters incl. "
        "federation-upstreams (rabbitmq.parameters), and policies "
        "(rabbitmq.policies). Answers 'is the shovel to the DR site "
        "running?' and 'what federation upstreams are configured?'. "
        "src-uri/dest-uri credentials are redacted. The parameter/policy "
        "surface needs the 'policymaker' user tag; status surfaces accept "
        "'monitoring'."
    ),
    "rabbitmq-definitions": (
        "Use to export the full RabbitMQ broker definitions "
        "(rabbitmq.definitions): every vhost, user, permission, exchange, "
        "queue, binding, parameter, and policy in one document — the "
        "backup/audit snapshot 'rabbitmqctl export_definitions' produces. "
        "User password hashes and shovel/federation URI credentials are "
        "redacted. Needs the 'administrator' user tag."
    ),
    "rabbitmq-raw": (
        "Use as an escape hatch to GET (or HEAD) an arbitrary read-only "
        "RabbitMQ Management API path the curated ops do not cover "
        "(rabbitmq.request) — e.g. a per-object detail endpoint like "
        "'/api/queues/{vhost}/{name}'. The connector refuses any method "
        "other than GET/HEAD before the request leaves the process. The "
        "required user tag depends on the path."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

#: The empty parameter object shared by the cluster-wide list ops that
#: take no arguments.
_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The optional ``vhost`` scoping param for endpoints that expose a
#: ``/{vhost}`` variant (queues, exchanges, bindings, policies, shovels).
_VHOST_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "Optional virtual host name to scope the listing to. Omit to list "
        "across every vhost the credential can see. The default vhost '/' "
        "is passed literally (the connector percent-encodes it)."
    ),
}

_VHOST_OPTIONAL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"vhost": _VHOST_PROPERTY},
    "additionalProperties": False,
}

#: The passthrough op's schema: a required read-only path plus an optional
#: method (GET/HEAD only) and query object.
_PASSTHROUGH_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "pattern": "^/",
            "description": (
                "The Management API path to read, e.g. "
                "'/api/queues/%2F/my-queue'. Must start with '/'. Percent-"
                "encode any '/' inside a vhost or name segment."
            ),
        },
        "method": {
            "type": "string",
            "enum": ["GET", "HEAD"],
            "description": "Read verb; defaults to GET. Any other verb is refused.",
        },
        "query": {
            "type": "object",
            "description": "Optional query-string parameters merged onto the request.",
            "additionalProperties": True,
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

#: The permissive response schema every op shares — RabbitMQ returns
#: either an object (overview / definitions) or an array (the collection
#: endpoints), so the reducer must accept both.
_ANY_RESPONSE: dict[str, Any] = {"type": ["object", "array", "null"]}


def _op(
    *,
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    group_key: str,
    user_tag: str,
    when_to_use: str,
    output_shape: str,
    parameter_schema: dict[str, Any] | None = None,
    extra_tags: tuple[str, ...] = (),
) -> RabbitMqOp:
    """Build a :class:`RabbitMqOp` with the shared read-only defaults.

    Keeps the op table declarative: every op is ``safety_level="safe"`` +
    ``requires_approval=False`` and carries the ``read-only`` / ``rabbitmq``
    tags plus the RabbitMQ *user tag* (``monitoring`` / ``policymaker`` /
    ``administrator``) the surface requires.
    """
    return RabbitMqOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        parameter_schema=parameter_schema if parameter_schema is not None else _EMPTY_PARAMS,
        response_schema=_ANY_RESPONSE,
        group_key=group_key,
        tags=("read-only", "rabbitmq", user_tag, *extra_tags),
        llm_instructions={"when_to_use": when_to_use, "output_shape": output_shape},
    )


RABBITMQ_OPS: tuple[RabbitMqOp, ...] = (
    # --- cluster identity + health (monitoring) ---------------------------
    _op(
        op_id="rabbitmq.overview",
        handler_attr="overview",
        summary="Whole-cluster overview: versions, rates, object totals, listeners.",
        description=(
            "GET /api/overview — the broad system snapshot: rabbitmq_version, "
            "erlang_version, management_version, cluster_name, aggregate "
            "message rates and object_totals (queues/exchanges/connections/"
            "consumers), and the node's listeners. The entry point for 'is "
            "this broker healthy and what version is it?'. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-cluster",
        user_tag="monitoring",
        when_to_use="Call first for cluster identity, version, and aggregate health.",
        output_shape="An object: {rabbitmq_version, cluster_name, object_totals, message_stats}.",
    ),
    _op(
        op_id="rabbitmq.nodes",
        handler_attr="nodes",
        summary="Per-node state: running flag, disc/ram type, resource alarms.",
        description=(
            "GET /api/nodes — one entry per cluster node with name, running "
            "(bool), type (disc/ram), mem_used/mem_limit/mem_alarm, "
            "disk_free/disk_free_alarm, fd_used, sockets_used, and (on recent "
            "releases) the per-node rabbitmq/erlang versions. Use to spot a "
            "down node or a node in a memory/disk alarm. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-cluster",
        user_tag="monitoring",
        when_to_use="Call to check per-node running state and resource alarms.",
        output_shape="An array of node objects: [{name, running, type, mem_alarm, ...}].",
    ),
    # --- messaging topology (monitoring) ----------------------------------
    _op(
        op_id="rabbitmq.exchanges",
        handler_attr="exchanges",
        summary="List exchanges (optionally scoped to a vhost).",
        description=(
            "GET /api/exchanges (or /api/exchanges/{vhost}) — each exchange's "
            "name, vhost, type (direct/fanout/topic/headers), durability, and "
            "auto_delete flag. Pass 'vhost' to scope. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-topology",
        user_tag="monitoring",
        when_to_use="Call to enumerate exchanges and their types/durability.",
        output_shape="An array of exchange objects: [{name, vhost, type, durable, ...}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
    ),
    _op(
        op_id="rabbitmq.queues",
        handler_attr="queues",
        summary="List queues with depth and consumer counts (optionally per vhost).",
        description=(
            "GET /api/queues (or /api/queues/{vhost}) — each queue's name, "
            "vhost, messages (ready + unacked), messages_ready, "
            "messages_unacknowledged, consumers, state, and node. The primary "
            "'how deep are the queues and are they being consumed?' op. Pass "
            "'vhost' to scope. safety_level=safe, read-only."
        ),
        group_key="rabbitmq-topology",
        user_tag="monitoring",
        when_to_use="Call to inspect queue depth, consumer counts, and state.",
        output_shape="An array of queue objects: [{name, vhost, messages, consumers, state, ...}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
    ),
    _op(
        op_id="rabbitmq.bindings",
        handler_attr="bindings",
        summary="List the exchange/queue binding graph (optionally per vhost).",
        description=(
            "GET /api/bindings (or /api/bindings/{vhost}) — each binding's "
            "source exchange, destination (queue or exchange), destination "
            "type, routing_key, and arguments. Use to reconstruct how "
            "messages route through the broker. Pass 'vhost' to scope. "
            "safety_level=safe, read-only."
        ),
        group_key="rabbitmq-topology",
        user_tag="monitoring",
        when_to_use="Call to see how exchanges and queues are wired together.",
        output_shape="An array of binding objects: [{source, destination, routing_key, ...}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
    ),
    _op(
        op_id="rabbitmq.vhosts",
        handler_attr="vhosts",
        summary="List virtual hosts and their aggregate message stats.",
        description=(
            "GET /api/vhosts — each virtual host's name, description, tags, "
            "and aggregate messages / message_stats. Use to enumerate the "
            "vhosts before scoping a topology op. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-topology",
        user_tag="monitoring",
        when_to_use="Call to list the virtual hosts on the broker.",
        output_shape="An array of vhost objects: [{name, description, messages, ...}].",
    ),
    # --- live connectivity (monitoring) -----------------------------------
    _op(
        op_id="rabbitmq.connections",
        handler_attr="connections",
        summary="List open client connections.",
        description=(
            "GET /api/connections — each open connection's name, peer host/"
            "port, user, vhost, protocol, state, and channel count. Use to "
            "see who is connected. safety_level=safe, read-only."
        ),
        group_key="rabbitmq-connectivity",
        user_tag="monitoring",
        when_to_use="Call to list open client connections and their users.",
        output_shape="An array of connection objects: [{name, user, vhost, state, ...}].",
    ),
    _op(
        op_id="rabbitmq.channels",
        handler_attr="channels",
        summary="List open channels.",
        description=(
            "GET /api/channels — each open channel's number, connection, user, "
            "vhost, consumer_count, and unacknowledged/prefetch counts. Use to "
            "drill from a connection into its channels. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-connectivity",
        user_tag="monitoring",
        when_to_use="Call to inspect open channels and their consumer/ack counts.",
        output_shape="An array of channel objects: [{name, user, consumer_count, ...}].",
    ),
    _op(
        op_id="rabbitmq.consumers",
        handler_attr="consumers",
        summary="List active consumers.",
        description=(
            "GET /api/consumers — each consumer's queue, channel, "
            "consumer_tag, ack_required, prefetch_count, and active flag. Use "
            "to answer 'which consumers are draining this queue?'. "
            "safety_level=safe, read-only."
        ),
        group_key="rabbitmq-connectivity",
        user_tag="monitoring",
        when_to_use="Call to list active consumers and the queues they drain.",
        output_shape="An array of consumer objects: [{queue, consumer_tag, active, ...}].",
    ),
    # --- shovel / federation / params (policymaker + status) --------------
    _op(
        op_id="rabbitmq.shovels",
        handler_attr="shovels",
        summary="List dynamic shovel definitions (credentials redacted).",
        description=(
            "GET /api/parameters/shovel (or /api/parameters/shovel/{vhost}) — "
            "the dynamic shovel runtime parameters: each shovel's name, vhost, "
            "and value (src-uri, dest-uri, src-queue, dest-queue, …). The "
            "src-uri/dest-uri AMQP userinfo (amqp://user:pass@host) is "
            "REDACTED to '***@host' before the result is returned. Pass "
            "'vhost' to scope. safety_level=safe, read-only."
        ),
        group_key="rabbitmq-federation",
        user_tag="policymaker",
        when_to_use="Call to read how dynamic shovels are configured (URIs redacted).",
        output_shape="An array of shovel-parameter objects: [{name, vhost, value:{src-uri, ...}}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
        extra_tags=("federation",),
    ),
    _op(
        op_id="rabbitmq.shovel_status",
        handler_attr="shovel_status",
        summary="Live status of dynamic and static shovels (credentials redacted).",
        description=(
            "GET /api/shovels (or /api/shovels/{vhost}) from the "
            "rabbitmq_shovel_management plugin — each shovel's name, vhost, "
            "type, and state (running/starting/terminated) plus src/dest "
            "endpoints. Any AMQP URI userinfo is REDACTED. Use to answer 'is "
            "the shovel to the DR site running?'. Pass 'vhost' to scope. "
            "safety_level=safe, read-only."
        ),
        group_key="rabbitmq-federation",
        user_tag="monitoring",
        when_to_use="Call to check whether shovels are currently running.",
        output_shape="An array of shovel-status objects: [{name, vhost, state, ...}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
        extra_tags=("federation",),
    ),
    _op(
        op_id="rabbitmq.federation_links",
        handler_attr="federation_links",
        summary="Live status of federation links (credentials redacted).",
        description=(
            "GET /api/federation-links from the rabbitmq_federation_management "
            "plugin — each federation link's upstream, vhost, type "
            "(exchange/queue), and status (running/starting/error). Any "
            "upstream AMQP URI userinfo is REDACTED. Use to see cross-site "
            "federation topology and health. safety_level=safe, read-only."
        ),
        group_key="rabbitmq-federation",
        user_tag="monitoring",
        when_to_use="Call to check federation link status across sites.",
        output_shape="An array of federation-link objects: [{upstream, vhost, status, ...}].",
        extra_tags=("federation",),
    ),
    _op(
        op_id="rabbitmq.parameters",
        handler_attr="parameters",
        summary="List runtime parameters incl. federation-upstreams (redacted).",
        description=(
            "GET /api/parameters — every runtime parameter across components "
            "(shovel, federation-upstream, …): component, name, vhost, and "
            "value. federation-upstream and shovel values embed AMQP URIs "
            "whose userinfo is REDACTED. Use to audit the full parameter set. "
            "safety_level=safe, read-only."
        ),
        group_key="rabbitmq-federation",
        user_tag="policymaker",
        when_to_use="Call to list all runtime parameters (URIs redacted).",
        output_shape="An array of parameter objects: [{component, name, vhost, value}].",
        extra_tags=("federation",),
    ),
    _op(
        op_id="rabbitmq.policies",
        handler_attr="policies",
        summary="List policies (optionally scoped to a vhost).",
        description=(
            "GET /api/policies (or /api/policies/{vhost}) — each policy's name, "
            "vhost, pattern, apply-to, priority, and definition (ha-mode, "
            "message-ttl, dead-letter routing, queue type, …). Use to audit "
            "queue/exchange guardrails. Pass 'vhost' to scope. "
            "safety_level=safe, read-only."
        ),
        group_key="rabbitmq-federation",
        user_tag="policymaker",
        when_to_use="Call to read the policies that govern queues/exchanges.",
        output_shape="An array of policy objects: [{name, vhost, pattern, definition}].",
        parameter_schema=_VHOST_OPTIONAL_PARAMS,
    ),
    # --- full export (administrator) --------------------------------------
    _op(
        op_id="rabbitmq.definitions",
        handler_attr="definitions",
        summary="Export the full broker definitions (secrets redacted).",
        description=(
            "GET /api/definitions — the whole-broker definitions document: "
            "rabbitmq_version, vhosts, users (with tags), permissions, "
            "topic_permissions, parameters, global_parameters, policies, "
            "queues, exchanges, and bindings. User password_hash values and "
            "any shovel/federation AMQP URI userinfo are REDACTED before the "
            "result is returned. safety_level=safe, read-only."
        ),
        group_key="rabbitmq-definitions",
        user_tag="administrator",
        when_to_use="Call for the full backup/audit snapshot of the broker (secrets redacted).",
        output_shape=(
            "An object: {rabbitmq_version, users, vhosts, permissions, parameters, "
            "policies, queues, exchanges, bindings}."
        ),
    ),
    # --- read-only escape hatch (path-dependent tag) ----------------------
    _op(
        op_id="rabbitmq.request",
        handler_attr="request_passthrough",
        summary="GET/HEAD an arbitrary read-only Management API path (redacted).",
        description=(
            "Reads an arbitrary Management API path the curated ops do not "
            "cover — e.g. a per-object detail endpoint "
            "'/api/queues/%2F/my-queue'. Only GET and HEAD are permitted; the "
            "connector refuses any other method before the request leaves the "
            "process. The response is passed through the same credential "
            "redaction as the shovel/definitions ops. safety_level=safe, "
            "read-only."
        ),
        group_key="rabbitmq-raw",
        user_tag="monitoring",
        when_to_use=(
            "Call only when no curated op covers the needed path; supply the full "
            "'/api/...' path. Non-GET/HEAD methods are refused."
        ),
        output_shape="The endpoint's JSON (object or array) for GET; a status summary for HEAD.",
        parameter_schema=_PASSTHROUGH_PARAMS,
    ),
)


#: Op ids whose handler runs the result through
#: :func:`~meho_backplane.connectors.rabbitmq.redact.redact_rabbitmq_payload`
#: — the shovel/federation/parameter/definitions surfaces that carry AMQP
#: URI credentials or password hashes, plus the arbitrary-path passthrough
#: (defence in depth: its path may reach any of the above). Exposed so the
#: tests can assert the redaction wiring stays in sync with the op table.
RABBITMQ_REDACTED_OP_IDS: frozenset[str] = frozenset(
    {
        "rabbitmq.shovels",
        "rabbitmq.shovel_status",
        "rabbitmq.federation_links",
        "rabbitmq.parameters",
        "rabbitmq.definitions",
        "rabbitmq.request",
    }
)
