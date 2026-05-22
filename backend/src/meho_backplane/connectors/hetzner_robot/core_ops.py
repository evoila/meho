# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hetzner Robot read-only v0.2 core — curated operator-enabled subset.

This module names the **10 read-only Hetzner Robot operations** the G3.7
Robot v0.2 ship enables out of the much larger Robot Webservice REST corpus
the G0.7 spec-ingestion pipeline lands under
``connector_id="hetzner-rest-2026.04"``. The curation is two-layered:

* :data:`ROBOT_CORE_GROUPS` — the operator-reviewed ``when_to_use``
  hint per LLM-grouping pass output group. Each entry's ``group_key``
  is the deterministic slug :func:`classify_robot_op` assigns to Robot
  ops; the ``when_to_use`` is what the agent reads verbatim through
  :func:`~meho_backplane.operations.meta_tools.list_operation_groups`
  to pick a group to search within.
* :data:`ROBOT_CORE_OPS` — the 10 ``EndpointDescriptor.op_id`` strings
  that flip to ``is_enabled=True`` at operator-review time, paired
  with the per-op ``llm_instructions`` blob the agent inlines into
  the reasoning context when it sees the op in
  :func:`~meho_backplane.operations.meta_tools.search_operations`
  hits. Every other op under the same connector triple stays
  ``is_enabled=False`` (the G0.7 ingestion default for
  ``source_kind='ingested'`` rows).

Per Initiative #370 and CLAUDE.md postulates 1-2, Hetzner Robot is
**fully generic-ingested**: the underlying ops are not registered in
code, they live in the ``endpoint_descriptor`` table. This module only
carries the **operator-review metadata** the substrate uses at the
review step — the actual curation is applied through
:func:`apply_robot_core_curation` against an existing ingested connector.

``ROBOT_PRODUCT`` / ``ROBOT_VERSION`` / ``ROBOT_IMPL_ID`` note
--------------------------------------------------------------

``ROBOT_PRODUCT = "hetzner"`` is the value
:func:`~meho_backplane.operations.ingest.parser.parse_connector_id`
extracts from ``ROBOT_CONNECTOR_ID = "hetzner-rest-2026.04"``:
``impl_id = "hetzner-rest"``; ``product = impl_id[:first_dash] =
"hetzner"``.

**Distinct from :attr:`HetznerRobotConnector.product` (``"hetzner-robot"``)**
— same discrepancy the SDDC Manager case documents
(``SddcManagerConnector.product="sddc-manager"`` but rows carry
``product="sddc"``). The ``OperationGroup`` and ``EndpointDescriptor``
rows use ``product="hetzner"`` so the :class:`ReviewService` scope
lookup (which calls ``parse_connector_id``) finds them. The connector
class's ``product="hetzner-robot"`` governs v2 registry lookup
(``(product, version, impl_id)`` triple) and is unrelated to the
row-level product key.

The 10 ops (paths cross-checked against the Hetzner Robot Webservice API
at https://robot.hetzner.com/doc/webservice/en.html):

1.  ``GET:/query`` — ``hetzner-robot.about`` — API version + account info.
2.  ``GET:/server`` — ``hetzner-robot.server.list`` — dedicated-server
    inventory for the account (server number, IP, product, datacenter).
3.  ``GET:/server/{server-ip}`` — ``hetzner-robot.server.info`` — single
    dedicated server detail.
4.  ``GET:/ip`` — ``hetzner-robot.ip.list`` — all IP addresses assigned to
    the account with their lock and traffic status.
5.  ``GET:/subnet`` — ``hetzner-robot.subnet.list`` — all subnets assigned
    to the account with gateway and IP version.
6.  ``GET:/vswitch`` — ``hetzner-robot.vswitch.list`` — all vSwitches with
    their server and VLAN memberships.
7.  ``GET:/vswitch/{id}`` — ``hetzner-robot.vswitch.info`` — single vSwitch
    detail including all member servers and VLANs.
8.  ``GET:/failover`` — ``hetzner-robot.failover.list`` — all failover IPs
    and their active routing target.
9.  ``GET:/rdns`` — ``hetzner-robot.rdns.list`` — all reverse DNS entries
    (PTR records) set on the account's IPs.
10. ``GET:/key`` — ``hetzner-robot.ssh_key.list`` — all SSH public keys
    registered in the Robot portal for the account.

Path families and group_keys
-----------------------------

Robot's REST paths are flat (one resource type per root segment). The
:data:`ROBOT_PATH_RULES` list maps each root path prefix to its
``group_key`` in most-specific-first order so the ``startswith`` loop
in :func:`classify_robot_op` terminates correctly even when one prefix
is a substring of another (``/ip`` vs ``/ip_address``).

Curation application
--------------------

:func:`apply_robot_core_curation` is the operator-review-time substrate
call that makes exactly the 10 curated ops dispatchable. Mirrors
:func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
verbatim, threading the "enable group but pin non-core ops disabled" needle
via the audit-log-driven operator-override exclusion.

server.list JSONFlux note
--------------------------

``GET:/server`` is the list op the acceptance test's JSONFlux
force-handle case dispatches. Hetzner Robot returns a top-level JSON
array (or a ``{"server": [...]}`` envelope depending on the endpoint
version normalised in :meth:`HetznerRobotConnector.fingerprint`). The
:func:`~tests.acceptance._robot_canary_fixtures.ForceHandleReducer`
handles the list shape; real JSONFlux reduction (MinIO/S3 spill,
``result_query`` meta-tool) is out of scope for v0.2 per Goal #214.

auth_failed invariant
----------------------

The Hetzner Robot API blocks the egress IP for 10 minutes after 3
consecutive 401 responses. The connector raises :exc:`RuntimeError`
with an ``auth_failed`` label on the **first** 401 — it never retries.
The acceptance test stub bypasses the Vault-backed credentials loader,
so no real 401 risk exists in test runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog

from meho_backplane.operations.ingest.service import ReviewService

__all__ = [
    "ROBOT_CONNECTOR_ID",
    "ROBOT_CORE_GROUPS",
    "ROBOT_CORE_OPS",
    "ROBOT_IMPL_ID",
    "ROBOT_PATH_RULES",
    "ROBOT_PRODUCT",
    "ROBOT_VERSION",
    "RobotCoreGroup",
    "RobotCoreOp",
    "apply_robot_core_curation",
    "classify_robot_op",
]

_log = structlog.get_logger(__name__)

#: Endpoint-descriptor product key — what
#: :func:`~meho_backplane.operations.ingest.parser.parse_connector_id`
#: extracts from ``"hetzner-rest-2026.04"`` (first hyphen-segment of
#: impl_id ``"hetzner-rest"``).
#:
#: **Distinct from :attr:`HetznerRobotConnector.product` (``"hetzner-robot"``).**
#: ``parse_connector_id("hetzner-rest-2026.04")`` → ``impl_id="hetzner-rest"``,
#: then ``product = impl_id[:first_dash] = "hetzner"``. The ``OperationGroup``
#: and ``EndpointDescriptor`` rows store ``product="hetzner"``; the connector
#: class's ``product="hetzner-robot"`` governs v2 registry lookup only.
#: This mirrors the SDDC Manager case (``SddcManagerConnector.product=
#: "sddc-manager"`` but rows carry ``product="sddc"``).
ROBOT_PRODUCT: Final[str] = "hetzner"
ROBOT_VERSION: Final[str] = "2026.04"
ROBOT_IMPL_ID: Final[str] = "hetzner-rest"

#: Connector-id slug the G0.6 dispatcher's ``parse_connector_id`` resolves
#: back to the (product, version, impl_id) triple above.
ROBOT_CONNECTOR_ID: Final[str] = f"{ROBOT_IMPL_ID}-{ROBOT_VERSION}"


@dataclass(frozen=True, slots=True)
class RobotCoreGroup:
    """One curated operator-review entry for a Hetzner Robot operation group.

    ``group_key`` is the slug :func:`classify_robot_op` emits.
    ``name`` is the operator-readable label ``meho connector review``
    renders. ``when_to_use`` is the agent-facing hint
    :func:`list_operation_groups` returns verbatim; every entry is a
    single complete sentence so the agent's group-selection step has
    unambiguous guidance.
    """

    group_key: str
    name: str
    when_to_use: str


@dataclass(frozen=True, slots=True)
class RobotCoreOp:
    """One curated operator-review entry for a Hetzner Robot operation.

    ``op_id`` follows the ``METHOD:path`` shape every
    ``source_kind='ingested'`` row uses; the path matches an entry in
    the Hetzner Robot Webservice OpenAPI spec.

    ``llm_instructions`` is the per-op JSON blob the meta-tools inline
    verbatim when the op surfaces. The shape (``when_to_call`` /
    ``output_shape`` / ``next_step``) mirrors the typed-connector
    convention from :mod:`meho_backplane.connectors.bind9.ops_zone`
    and :mod:`meho_backplane.connectors.harbor.core_ops` — same agent
    reads both surfaces, so the structure stays uniform.
    """

    op_id: str
    group_key: str
    llm_instructions: dict[str, object]


#: Path-prefix → group_key classifier rules for Hetzner Robot.
#:
#: **Order is load-bearing.** Each rule is checked via
#: ``path.startswith(prefix)``. More-specific prefixes must precede
#: less-specific ones where overlap exists (e.g. ``/vswitch/{id}``
#: before ``/vswitch``). The rules encode every root-level path the
#: 10 curated ops use; paths outside these prefixes are un-curated
#: and stay ``is_enabled=False`` after :func:`apply_robot_core_curation`
#: runs.
ROBOT_PATH_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("/query", "robot-about"),
    # Server family
    ("/server", "robot-servers"),
    # Networking — IP, subnet, vSwitch, failover, rDNS
    ("/ip", "robot-networking"),
    ("/subnet", "robot-networking"),
    ("/vswitch", "robot-networking"),
    ("/failover", "robot-networking"),
    ("/rdns", "robot-networking"),
    # SSH keys
    ("/key", "robot-ssh-keys"),
)


def classify_robot_op(op_id: str) -> str:
    """Return the curated ``group_key`` for a Robot op_id, or ``"none"``.

    ``op_id`` is the ``METHOD:/path`` form ingested rows carry; the
    helper strips the verb and matches the path against
    :data:`ROBOT_PATH_RULES` in order.

    Only ``GET`` verbs are considered curated (all 10 core ops are
    read-only). A non-GET op or a path outside the curated families
    returns ``"none"`` — those rows stay ``is_enabled=False``.

    Returns ``"none"`` for paths outside the curated families (e.g.
    ``/boot``, ``/reset``, ``/wol``); those rows are un-curated and
    stay ``is_enabled=False`` after :func:`apply_robot_core_curation`
    runs.
    """
    try:
        method, path = op_id.split(":", 1)
    except ValueError:
        return "none"
    if method != "GET":
        return "none"
    for prefix, group_key in ROBOT_PATH_RULES:
        if path.startswith(prefix):
            return group_key
    return "none"


def _instructions(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
) -> dict[str, object]:
    """Build the per-op ``llm_instructions`` blob with the canonical keys.

    Same three-field shape :mod:`meho_backplane.connectors.harbor.core_ops`
    and :mod:`meho_backplane.connectors.nsx.core_ops` use so an agent
    crossing connector boundaries sees a stable convention.
    """
    return {
        "when_to_call": when_to_call,
        "output_shape": output_shape,
        "next_step": next_step,
    }


#: Operator-reviewed ``when_to_use`` hints for the 4 Hetzner Robot groups
#: the read-only v0.2 core spans. Every hint is one complete sentence the
#: agent reads verbatim — vague hints poison ``search_operations`` ranking.
ROBOT_CORE_GROUPS: Final[tuple[RobotCoreGroup, ...]] = (
    RobotCoreGroup(
        group_key="robot-about",
        name="Hetzner Robot (about)",
        when_to_use=(
            "Use this group to read Hetzner Robot API-level information: the "
            "API version and the account-level summary. The lightweight probe "
            "surface the agent calls first to confirm the Robot Webservice is "
            "reachable and to determine which account is configured."
        ),
    ),
    RobotCoreGroup(
        group_key="robot-servers",
        name="Hetzner Robot Dedicated Servers",
        when_to_use=(
            "Use this group to list or inspect dedicated servers under the Hetzner "
            "Robot account. Each server entry carries the server number, primary IP, "
            "product name, datacenter, and traffic plan. Use when answering 'what "
            "dedicated servers does this account own', 'what datacenter is server X "
            "in', or 'what is the product type of server Y'."
        ),
    ),
    RobotCoreGroup(
        group_key="robot-networking",
        name="Hetzner Robot Networking",
        when_to_use=(
            "Use this group to inspect the networking resources assigned to the "
            "Hetzner Robot account: IP addresses (with lock and traffic status), "
            "subnets (with gateway and IP version), vSwitches (with VLAN and server "
            "memberships), failover IPs (with active routing targets), and reverse "
            "DNS entries (PTR records). Use when answering questions about IP "
            "assignments, routing, network topology, or DNS resolution for any "
            "resource in the account."
        ),
    ),
    RobotCoreGroup(
        group_key="robot-ssh-keys",
        name="Hetzner Robot SSH Keys",
        when_to_use=(
            "Use this group to list SSH public keys registered in the Hetzner Robot "
            "portal for the account. Each entry carries the key fingerprint, name, "
            "and type. Use when auditing which SSH keys are registered for server "
            "provisioning, checking key presence before a reinstall, or confirming "
            "a key is available for a specific operator."
        ),
    ),
)


#: The 10 curated read-only Hetzner Robot core ops. Each entry carries
#: the op_id (``GET:/path`` form), the curated group assignment, and the
#: operator-reviewed ``llm_instructions`` blob.
#:
#: Paths cross-checked against the Hetzner Robot Webservice API at
#: https://robot.hetzner.com/doc/webservice/en.html.
ROBOT_CORE_OPS: Final[tuple[RobotCoreOp, ...]] = (
    # ---- About ----
    RobotCoreOp(
        op_id="GET:/query",
        group_key="robot-about",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the Hetzner Robot API version and high-level account "
                "summary. Use as the first probe when connecting to a new Robot "
                "target to confirm reachability and identify the account. Returns "
                "API version and account information."
            ),
            output_shape=(
                "Object with api_version (string) and account-level summary fields "
                "including the account identifier and any quota information the "
                "Robot Webservice exposes at this endpoint."
            ),
            next_step=(
                "Proceed to hetzner-robot.server.list for the dedicated-server "
                "inventory, or to hetzner-robot.ip.list for the network resource "
                "overview."
            ),
        ),
    ),
    # ---- Servers ----
    RobotCoreOp(
        op_id="GET:/server",
        group_key="robot-servers",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all dedicated servers owned by the Hetzner Robot "
                "account. Returns the full inventory with server number, primary "
                "IP, product name, datacenter, and traffic plan. Large accounts "
                "may return many servers; the response is a JSON array. Large "
                "lists return a JSONFlux handle through the shared HandleStore."
            ),
            output_shape=(
                "Array of Server objects (or a {'server': [...]} envelope); each "
                "carries server_ip (primary IP string), server_number (int), "
                "server_name (optional label), product (product string, e.g. "
                "'AX41-NVMe'), dc (datacenter name), traffic (traffic plan string), "
                "flatrate (bool), status ('ready' or other), throttled (bool), "
                "cancelled (bool), and paid_until (ISO date string)."
            ),
            next_step=(
                "Use server_ip as the path parameter for "
                "hetzner-robot.server.info to get the full detail of one server. "
                "Cross-reference with hetzner-robot.ip.list to map the server's "
                "IP addresses to their lock and traffic status."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/server/{server-ip}",
        group_key="robot-servers",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one dedicated server by its "
                "primary IP address. Requires server_ip obtained from "
                "hetzner-robot.server.list. Returns the same fields as the list "
                "op plus additional hardware and network detail the list view "
                "may omit."
            ),
            output_shape=(
                "Server object with server_ip, server_number, server_name, "
                "product, dc, traffic, flatrate (bool), status, throttled (bool), "
                "cancelled (bool), and paid_until."
            ),
            next_step=(
                "Cross-reference the server_ip with hetzner-robot.ip.list to "
                "see all IPs assigned to this server, or with "
                "hetzner-robot.vswitch.list to check vSwitch membership."
            ),
        ),
    ),
    # ---- Networking ----
    RobotCoreOp(
        op_id="GET:/ip",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all IP addresses assigned to the Hetzner Robot "
                "account. Returns every IP with its lock status, traffic quota, "
                "and the server it is routed to. Use when answering 'what IPs "
                "does this account own', 'which IPs are locked', or as the entry "
                "point before inspecting subnets or failover routing."
            ),
            output_shape=(
                "Array of IP objects; each carries ip (string), server_ip "
                "(the primary IP of the server it belongs to), locked (bool), "
                "separate_mac (bool or null), traffic_warnings (bool), "
                "traffic_hourly (int or null), traffic_daily (int or null), "
                "and traffic_monthly (int or null)."
            ),
            next_step=(
                "Pick an ip value for the per-IP detail endpoint if available, "
                "or cross-reference with hetzner-robot.failover.list to check "
                "if any listed IPs are used as failover targets."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/subnet",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all subnets (CIDR blocks) assigned to the Hetzner "
                "Robot account. Returns each subnet's CIDR, gateway, IP version, "
                "and the server it is routed to. Use when answering 'what subnets "
                "does this account have', 'what is the gateway for subnet X', or "
                "to map network topology before configuring routing."
            ),
            output_shape=(
                "Array of Subnet objects; each carries ip (CIDR string, e.g. "
                "'2a01:4f8::/29'), mask (prefix length int), gateway (string), "
                "server_ip (the primary IP of the server the subnet routes to), "
                "and ip_version (4 or 6)."
            ),
            next_step=(
                "Cross-reference the server_ip with hetzner-robot.server.info "
                "to confirm which physical server hosts the subnet, or with "
                "hetzner-robot.rdns.list to check PTR records for IPs in the "
                "subnet."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/vswitch",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all vSwitches configured in the Hetzner Robot "
                "account. Each vSwitch entry includes its ID, name, VLAN, and "
                "the servers that are members. Use when answering 'what vSwitches "
                "exist', 'which servers are in vSwitch X', or to audit VLAN "
                "topology across the account."
            ),
            output_shape=(
                "Array of vSwitch objects; each carries id (int), name (string), "
                "vlan (VLAN ID int), cancelled (bool), and server[] (list of "
                "server objects each with server_ip, server_number, and status)."
            ),
            next_step=(
                "Use the vSwitch id for hetzner-robot.vswitch.info to get the "
                "full detail including all VLAN assignments and member server "
                "statuses."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/vswitch/{id}",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to read the full detail of one vSwitch by its numeric ID. "
                "Requires id obtained from hetzner-robot.vswitch.list. Returns "
                "the same fields as the list op plus any additional VLAN or "
                "server membership detail the list view may omit."
            ),
            output_shape=(
                "vSwitch object with id, name, vlan (VLAN ID int), cancelled "
                "(bool), and server[] (each with server_ip, server_number, "
                "and status string)."
            ),
            next_step=(
                "Cross-reference the server entries against "
                "hetzner-robot.server.info for each server_ip to confirm the "
                "physical server details. Check hetzner-robot.ip.list to map "
                "additional IPs on those servers."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/failover",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all failover IPs configured in the Hetzner Robot "
                "account. Each entry shows which server the failover IP currently "
                "routes to (active_server_ip) and which server the IP belongs to "
                "(server_ip). Use when auditing HA routing, confirming a failover "
                "is active on the intended server, or investigating an outage "
                "where traffic routing is suspect."
            ),
            output_shape=(
                "Array of Failover objects; each carries ip (the failover IP "
                "string), server_ip (the owning server's primary IP), "
                "active_server_ip (the server currently receiving traffic), and "
                "netmask (string)."
            ),
            next_step=(
                "Compare server_ip vs active_server_ip — if they differ, the "
                "failover IP is currently routed away from its primary server. "
                "Cross-reference with hetzner-robot.server.info to confirm both "
                "servers are in 'ready' status."
            ),
        ),
    ),
    RobotCoreOp(
        op_id="GET:/rdns",
        group_key="robot-networking",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all reverse DNS (PTR record) entries configured "
                "in the Hetzner Robot account. Returns every IP whose PTR record "
                "has been explicitly set. Use when auditing DNS configuration, "
                "confirming a PTR record is set correctly, or investigating mail "
                "delivery issues where reverse DNS validation is failing."
            ),
            output_shape=(
                "Array of RDNS objects; each carries ip (the IP address string "
                "whose PTR is set) and ptr (the hostname string the PTR resolves "
                "to)."
            ),
            next_step=(
                "Cross-reference the ip values against hetzner-robot.ip.list "
                "to confirm the IP is still assigned to the account, or against "
                "hetzner-robot.server.info to see which server holds the IP."
            ),
        ),
    ),
    # ---- SSH Keys ----
    RobotCoreOp(
        op_id="GET:/key",
        group_key="robot-ssh-keys",
        llm_instructions=_instructions(
            when_to_call=(
                "Call to list all SSH public keys registered in the Hetzner Robot "
                "portal for the account. Returns each key's name, fingerprint, and "
                "type. Use when auditing which SSH keys are available for server "
                "reinstalls, checking whether a specific key is registered before "
                "requesting a reinstall, or confirming an operator's key is "
                "present."
            ),
            output_shape=(
                "Array of Key objects; each carries fingerprint (MD5 hex string), "
                "name (operator-assigned label), type (key type string, e.g. "
                "'ED25519' or 'RSA'), and size (int, key size in bits)."
            ),
            next_step=(
                "Use the fingerprint when referencing a specific key in a "
                "reinstall request. If the expected key is absent, the key must "
                "be added in the Robot portal before any reinstall that depends "
                "on it can proceed."
            ),
        ),
    ),
)


async def apply_robot_core_curation(
    review_service: ReviewService,
    *,
    tenant_id: UUID | None,
) -> None:
    """Apply the curated 10-op read core against an ingested Robot connector.

    Drives the substrate so that, after this call returns, exactly
    the 10 ops in :data:`ROBOT_CORE_OPS` are dispatchable
    (``is_enabled=True``) and every other ingested op stays
    ``is_enabled=False``. The 4 curated groups land
    ``review_status='enabled'`` so the agent's
    :func:`~meho_backplane.operations.meta_tools.search_operations`
    surfaces the core ops; non-curated groups are left untouched
    (``review_status='staged'`` from the G0.7 ingest default).

    The substrate doesn't expose "enable only ops X, Y, Z under
    group G": :meth:`ReviewService.enable_group`'s cascade flips
    ``is_enabled=True`` on every child op in the group. The helper
    works around this via the audit-log-driven operator-override
    exclusion — the same mechanism
    :func:`~meho_backplane.connectors.harbor.core_ops.apply_harbor_core_curation`
    and :func:`~meho_backplane.connectors.nsx.core_ops.apply_nsx_core_curation`
    established:

    1. :meth:`ReviewService.get_review_payload` loads the current
       state of every curated group and its child ops.
    2. For each child op in a curated group that **isn't** in the
       :data:`ROBOT_CORE_OPS` allow-list,
       :meth:`ReviewService.edit_op` with ``is_enabled=False``
       writes the operator-override audit row. The follow-on
       :meth:`enable_group` cascade detects these rows and skips
       them.
    3. :meth:`ReviewService.edit_group` lands the operator-reviewed
       ``name`` + ``when_to_use`` on each curated group.
    4. :meth:`ReviewService.enable_group` flips
       ``review_status='enabled'`` and cascades ``is_enabled=True``
       to the curated child ops (operator-overridden non-core ops
       are skipped).
    5. :meth:`ReviewService.edit_op` lands the curated
       ``llm_instructions`` blob per entry in :data:`ROBOT_CORE_OPS`.

    Raises :class:`~meho_backplane.operations.ingest.ConnectorNotFoundError`
    if no groups exist for ``hetzner-rest-2026.04`` under *tenant_id*
    (the operator must run ``meho connector ingest`` against the Robot
    spec before this helper applies).
    """
    payload = await review_service.get_review_payload(
        ROBOT_CONNECTOR_ID,
        tenant_id,
    )

    core_op_ids_by_group: dict[str, set[str]] = {}
    for op in ROBOT_CORE_OPS:
        core_op_ids_by_group.setdefault(op.group_key, set()).add(op.op_id)

    for group_payload in payload.groups:
        allow_list = core_op_ids_by_group.get(group_payload.group_key)
        if allow_list is None:
            continue
        for review_op in group_payload.ops:
            if review_op.op_id in allow_list:
                continue
            await review_service.edit_op(
                ROBOT_CONNECTOR_ID,
                review_op.op_id,
                tenant_id=tenant_id,
                is_enabled=False,
            )
            _log.info(
                "robot_non_core_op_disabled",
                connector_id=ROBOT_CONNECTOR_ID,
                op_id=review_op.op_id,
                group_key=group_payload.group_key,
            )

    for group in ROBOT_CORE_GROUPS:
        await review_service.edit_group(
            ROBOT_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
            name=group.name,
            when_to_use=group.when_to_use,
        )
        await review_service.enable_group(
            ROBOT_CONNECTOR_ID,
            group.group_key,
            tenant_id=tenant_id,
        )
        _log.info(
            "robot_core_group_enabled",
            connector_id=ROBOT_CONNECTOR_ID,
            group_key=group.group_key,
        )

    for op in ROBOT_CORE_OPS:
        await review_service.edit_op(
            ROBOT_CONNECTOR_ID,
            op.op_id,
            tenant_id=tenant_id,
            llm_instructions=op.llm_instructions,
        )
        _log.info(
            "robot_core_op_curated",
            connector_id=ROBOT_CONNECTOR_ID,
            op_id=op.op_id,
        )
