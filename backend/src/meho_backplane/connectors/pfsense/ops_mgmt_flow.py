# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""pfSense management-plane flow classification op (meho-internal#252).

Adds one governed read op to :class:`PfSenseConnector`:

* ``pfsense.mgmt_flow.summary`` -- runs ``pfctl -ss`` over SSH, then
  classifies every live TCP connection state whose server side is a
  management port (443/22/902/5480 class) sitting in a caller-supplied
  management network into **sanctioned** vs **non-sanctioned** by source,
  and further flags **unexpected** sources (non-sanctioned AND not in a
  caller-supplied known/baseline set). Returns a compact per-leg summary
  plus the distinct offending source sets.

Why a governed op (and why the state table)
--------------------------------------------

The management-plane lockdown ratchet (Goal meho-internal#234) stage 1
put passive floating ``match`` counting rules on the lab pfSense and an
on-demand report over an SSH script (kb
``pfsense-2.7-management-plane-flow-counting.md``). Stage 2 (Initiative
#249, Task #252) makes that signal *active* by pinning it to a MEHO
Sensor. A Sensor's assertion is a **bounded** select (one dotted path +
at most one aggregate + one typed comparator, #2504) -- it cannot itself
filter a raw state table by source-set membership. So the classification
must happen inside a governed op that returns a pre-classified, compact,
assertable result. This module is that op.

It classifies the **live state table** (``pfctl -ss``), exactly like the
report's zero-change ``states`` mode: per-source attribution is inherent
in the state table, so no firewall rule / logging change is required (the
Task's acceptance criterion forbids any forwarding change). The trade-off
is that the state table is an instantaneous snapshot of *currently open*
connections -- a brief out-of-band connection that opens and closes
between Sensor ticks is not captured. A cumulative logged-counter source
is a separate, firewall-changing stage-2b decision and is intentionally
out of scope here.

Lab-agnostic on purpose
------------------------

No lab CIDRs or hostnames are baked in. ``sanctioned_src`` and
``mgmt_nets`` are **required** params; ``mgmt_ports`` defaults to the
vendor-generic 443/22/902/5480 class; ``baseline_src`` is optional. The
lab-specific values live in the Sensor's ``params`` (a backplane object),
not in this public connector, so the same op governs any future domain.

Pure classifier vs handler thin layer
--------------------------------------

Following the :mod:`~meho_backplane.connectors.pfsense.ops_read`
convention, :func:`classify_mgmt_flows` is a pure function tested directly
against fixture text; the bound-method handler is the thin SSH-call +
parse + classify layer. The parser it consumes
(:func:`~meho_backplane.connectors.pfsense.ops_read.parse_pfctl_states`)
is reused, not duplicated.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.ops_read import parse_pfctl_states

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.pfsense.connector import PfSenseConnector

__all__ = [
    "MGMT_FLOW_OPS",
    "MGMT_FLOW_SUMMARY_OP",
    "classify_mgmt_flows",
    "pfsense_mgmt_flow_summary",
]

#: The vendor-generic management-port class (https UI/API, ssh, esxi
#: vpxa/nfc, vami). Safe as a default because these are product ports, not
#: lab-specific addresses.
_DEFAULT_MGMT_PORTS: tuple[int, ...] = (443, 22, 902, 5480)

#: Cap on the (potentially large -- one per operator) ``non_sanctioned_sources``
#: list so the summary payload stays inline (never JSONFlux-handle-wrapped,
#: which would hide the fields a Sensor asserts on). The distinct **counts**
#: are always exact; only the enumerated list is truncated. ``unexpected_sources``
#: is intentionally *not* capped -- a new out-of-band source is the rare signal
#: the Sensor exists to name, so it must be complete.
_MAX_NON_SANCTIONED_SOURCES = 200

#: The measurement caveat, surfaced on every result (mirrors the stage-1
#: report). A pfSense only sees a flow it routes between two of its own
#: segments; same-subnet flows (e.g. a same-subnet governed backplane read)
#: never transit it and are invisible here.
_CAVEAT = (
    "Only flows that CROSS the pfSense are classified. Same-subnet flows "
    "(source and destination on one pfSense segment) do not transit it and "
    "are invisible here, so a low sanctioned count on a physical-mgmt leg is "
    "expected. What is measured reliably is remote (operator) reach."
)


def _split_host_port(endpoint: str | None) -> tuple[str, int] | None:
    """Split a pfctl state endpoint (``ip:port`` or ``[v6]:port``).

    Returns ``(host, port)`` or ``None`` when the endpoint is missing or
    unparseable (the host is validated as an IP address by the caller).
    """
    if not endpoint:
        return None
    ep = endpoint.strip()
    if ep.startswith("["):  # bracketed IPv6: [2001:db8::1]:443
        host, sep, port = ep[1:].partition("]:")
    else:
        host, sep, port = ep.rpartition(":")
    if not sep:
        return None
    try:
        return host, int(port)
    except ValueError:
        return None


def _compile_nets(sources: Any) -> list[Any]:
    """Compile a list of CIDR/host strings into ``ip_network`` objects.

    Unparseable entries are dropped (host strings without a prefix become
    ``/32`` or ``/128`` via ``strict=False``).
    """
    nets: list[Any] = []
    for src in sources or []:
        try:
            nets.append(ipaddress.ip_network(str(src).strip(), strict=False))
        except ValueError:
            continue
    return nets


def _ip_in_nets(ip_obj: Any, nets: list[Any]) -> bool:
    """True when *ip_obj* falls in any network of the same IP version."""
    return any(ip_obj.version == net.version and ip_obj in net for net in nets)


def _leg_for(ip_obj: Any, leg_nets: list[tuple[Any, str]]) -> str | None:
    """Return the leg label of the first ``mgmt_nets`` CIDR *ip_obj* falls in."""
    for net, leg in leg_nets:
        if ip_obj.version == net.version and ip_obj in net:
            return leg
    return None


def _compile_leg_nets(mgmt_nets: Any) -> list[tuple[Any, str]]:
    """Compile ``[{"cidr","leg"}, ...]`` into ``[(network, leg_label), ...]``."""
    leg_nets: list[tuple[Any, str]] = []
    for entry in mgmt_nets or []:
        if not isinstance(entry, dict) or "cidr" not in entry:
            continue
        try:
            net = ipaddress.ip_network(str(entry["cidr"]).strip(), strict=False)
        except ValueError:
            continue
        leg_nets.append((net, str(entry.get("leg") or entry["cidr"])))
    return leg_nets


def _server_client_split(
    src: tuple[str, int], dst: tuple[str, int], ports: set[int]
) -> tuple[str, int, str] | None:
    """Pick the server side (endpoint whose port is a management port).

    Returns ``(server_ip, server_port, client_ip)`` or ``None`` when
    neither endpoint's port is a management port.
    """
    src_ip, src_port = src
    dst_ip, dst_port = dst
    if dst_port in ports:
        return dst_ip, dst_port, src_ip
    if src_port in ports:
        return src_ip, src_port, dst_ip
    return None


def _summarize_sources(
    agg: dict[tuple[str, str], dict[str, Any]], *, cap: int | None
) -> tuple[list[dict[str, Any]], bool]:
    """Render a ``{(src, leg): {ports, states}}`` aggregate into sorted rows."""
    rows = [
        {"src": src, "leg": leg, "ports": sorted(v["ports"]), "states": v["states"]}
        for (src, leg), v in agg.items()
    ]
    rows.sort(key=lambda r: (r["leg"], r["src"]))
    if cap is not None and len(rows) > cap:
        return rows[:cap], True
    return rows, False


def _build_summary(
    *,
    leg_stats: dict[str, dict[str, int]],
    non_sanctioned: dict[tuple[str, str], dict[str, Any]],
    unexpected: dict[tuple[str, str], dict[str, Any]],
    sanctioned_source_count: int,
    total: int,
    sanctioned_states: int,
    non_sanctioned_states: int,
    unparsed: int,
) -> dict[str, Any]:
    """Render the classifier's accumulators into the final summary object."""
    ns_list, ns_trunc = _summarize_sources(non_sanctioned, cap=_MAX_NON_SANCTIONED_SOURCES)
    ux_list, _ = _summarize_sources(unexpected, cap=None)
    legs_out = {
        leg: {
            "sanctioned_states": st["sanctioned_states"],
            "non_sanctioned_states": st["non_sanctioned_states"],
            "coverage_pct": (
                round(100.0 * st["sanctioned_states"] / tot, 1)
                if (tot := st["sanctioned_states"] + st["non_sanctioned_states"])
                else None
            ),
        }
        for leg, st in sorted(leg_stats.items())
    }
    grand = sanctioned_states + non_sanctioned_states
    return {
        "legs": legs_out,
        "total_states_classified": total,
        "unparsed_lines": unparsed,
        "sanctioned_state_count": sanctioned_states,
        "non_sanctioned_state_count": non_sanctioned_states,
        "sanctioned_source_count": sanctioned_source_count,
        "non_sanctioned_source_count": len(non_sanctioned),
        "unexpected_source_count": len(unexpected),
        "coverage_pct": round(100.0 * sanctioned_states / grand, 1) if grand else None,
        "non_sanctioned_sources": ns_list,
        "non_sanctioned_sources_truncated": ns_trunc,
        "unexpected_sources": ux_list,
        "caveat": _CAVEAT,
    }


def classify_mgmt_flows(
    state_rows: list[dict[str, Any]],
    *,
    sanctioned_src: Any,
    mgmt_nets: Any,
    mgmt_ports: Any = None,
    baseline_src: Any = None,
) -> dict[str, Any]:
    """Classify parsed ``pfctl -ss`` rows into a compact governance summary.

    A state is *classified* when it is TCP, both endpoints parse, its
    server side (the management-port endpoint) sits in one of ``mgmt_nets``,
    and the client side is therefore the source under scrutiny. The client
    is **sanctioned** when it is in ``sanctioned_src``, else
    **non-sanctioned**; a non-sanctioned client that is also not in
    ``baseline_src`` is **unexpected** -- the new-source signal a Sensor
    asserts on (``$.unexpected_source_count`` scalar, or an aggregate
    ``count`` over ``$.unexpected_sources`` so the breach evidence names
    each ``{src, leg, ports}``).

    A state line the parser could not structure (``proto`` is ``None`` --
    a truncated/unknown form or a NAT-translated first endpoint) is counted
    in ``unparsed_lines`` and never classified, so an unrecognised state can
    never be silently absorbed into a clean summary. A Sensor pins
    ``$.unparsed_lines <= 0`` alongside the source assertion to catch that.
    """
    ports = {int(p) for p in mgmt_ports} if mgmt_ports else set(_DEFAULT_MGMT_PORTS)
    sanctioned_nets = _compile_nets(sanctioned_src)
    baseline_nets = _compile_nets(baseline_src)
    leg_nets = _compile_leg_nets(mgmt_nets)

    leg_stats: dict[str, dict[str, int]] = {}
    non_sanctioned: dict[tuple[str, str], dict[str, Any]] = {}
    unexpected: dict[tuple[str, str], dict[str, Any]] = {}
    sanctioned_sources: set[tuple[str, str]] = set()
    total = sanctioned_states = non_sanctioned_states = 0
    unparsed = 0

    for row in state_rows:
        proto = row.get("proto")
        if proto is None:
            # A line ``parse_pfctl_states`` could not structure at all: a
            # truncated / unknown form, or a NAT-translated first endpoint
            # (``addr:port (xlate:port) <dir> ...`` -- out of scope for this
            # port-based classifier). Counted in ``unparsed_lines`` so a pinned
            # Sensor can assert ``$.unparsed_lines <= 0`` and treat an
            # unrecognised state as a breach signal, NOT a silent all-clear.
            # Never counted as classified.
            unparsed += 1
            continue
        if proto != "tcp":
            continue
        src_hp = _split_host_port(row.get("src"))
        dst_hp = _split_host_port(row.get("dst"))
        if not src_hp or not dst_hp:
            continue
        split = _server_client_split(src_hp, dst_hp, ports)
        if split is None:
            continue
        server_ip_s, server_port, client_ip_s = split
        try:
            server_ip = ipaddress.ip_address(server_ip_s)
            client_ip = ipaddress.ip_address(client_ip_s)
        except ValueError:
            continue
        leg = _leg_for(server_ip, leg_nets)
        if leg is None:
            continue
        total += 1
        stats = leg_stats.setdefault(leg, {"sanctioned_states": 0, "non_sanctioned_states": 0})
        if _ip_in_nets(client_ip, sanctioned_nets):
            stats["sanctioned_states"] += 1
            sanctioned_states += 1
            sanctioned_sources.add((client_ip_s, leg))
            continue
        stats["non_sanctioned_states"] += 1
        non_sanctioned_states += 1
        ent = non_sanctioned.setdefault((client_ip_s, leg), {"ports": set(), "states": 0})
        ent["ports"].add(server_port)
        ent["states"] += 1
        if not _ip_in_nets(client_ip, baseline_nets):
            uent = unexpected.setdefault((client_ip_s, leg), {"ports": set(), "states": 0})
            uent["ports"].add(server_port)
            uent["states"] += 1

    return _build_summary(
        leg_stats=leg_stats,
        non_sanctioned=non_sanctioned,
        unexpected=unexpected,
        sanctioned_source_count=len(sanctioned_sources),
        total=total,
        sanctioned_states=sanctioned_states,
        non_sanctioned_states=non_sanctioned_states,
        unparsed=unparsed,
    )


async def pfsense_mgmt_flow_summary(
    self: PfSenseConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Return the management-plane flow classification summary.

    Op-id: ``pfsense.mgmt_flow.summary``. Reads the live state table and
    collapses it to a compact per-leg summary plus the distinct
    non-sanctioned / unexpected source sets. A failed ``pfctl -ss`` read
    (non-zero exit with no output) **raises** so the dispatch fails and a
    pinned Sensor evaluates ``unknown`` rather than reading a false
    all-clear (sensor contract: a refusal must fail the dispatch, not
    return a reading).
    """
    proc = await self._run_command(target, "pfctl -ss", operator=operator)
    stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    content = stdout if isinstance(stdout, str) else ""
    if proc.exit_status != 0 and not content.strip():
        raise RuntimeError(f"pfctl -ss failed: exit {proc.exit_status}")
    rows = parse_pfctl_states(content)
    return classify_mgmt_flows(
        rows,
        sanctioned_src=params.get("sanctioned_src") or [],
        mgmt_nets=params.get("mgmt_nets") or [],
        mgmt_ports=params.get("mgmt_ports"),
        baseline_src=params.get("baseline_src") or [],
    )


_WHEN_TO_USE_MGMT_FLOW = (
    "Use to measure out-of-band management-plane reach through a pfSense: "
    "which sources are reaching appliance management ports (443/22/902/5480 "
    "class) across the firewall, split into sanctioned (governed backplane / "
    "satellite / island tooling) vs non-sanctioned, with the non-sanctioned "
    "sources that are not in a known baseline flagged as 'unexpected'. Pin it "
    "to a Sensor (assert ``$.unexpected_source_count <= 0``, or aggregate "
    "``count`` over ``$.unexpected_sources`` so the breach names each source; "
    "pin ``$.unparsed_lines <= 0`` alongside so an unrecognised state line is "
    "not silently read as an all-clear) to alert when a NEW out-of-band source "
    "appears. Read-only; classifies the live state table and changes nothing "
    "on the firewall."
)

_MGMT_NET_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "cidr": {"type": "string", "description": "A management-network CIDR (or host/32)."},
        "leg": {"type": "string", "description": "A label for the leg this CIDR egresses."},
    },
    "required": ["cidr", "leg"],
    "additionalProperties": False,
}

_SOURCE_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string"},
        "leg": {"type": "string"},
        "ports": {"type": "array", "items": {"type": "integer"}},
        "states": {"type": "integer"},
    },
    "additionalProperties": True,
}

MGMT_FLOW_SUMMARY_OP = PfSenseOp(
    op_id="pfsense.mgmt_flow.summary",
    handler_attr="mgmt_flow_summary",
    summary=(
        "Classify live management-plane flows from the pfSense state table "
        "into sanctioned / non-sanctioned / unexpected sources."
    ),
    description=(
        "Runs ``pfctl -ss`` over SSH and classifies every live TCP "
        "connection state whose server side is a management port "
        "(``mgmt_ports``, default 443/22/902/5480) sitting in one of the "
        "caller-supplied ``mgmt_nets`` by source: **sanctioned** when the "
        "client source is in ``sanctioned_src``, else **non-sanctioned**; a "
        "non-sanctioned source not in ``baseline_src`` is **unexpected**. "
        "Returns a compact per-leg summary (open-state counts + coverage %) "
        "plus the distinct ``non_sanctioned_sources`` (capped) and "
        "``unexpected_sources`` (complete) as ``{src, leg, ports, states}`` "
        "rows, and the ``*_source_count`` scalars, plus an ``unparsed_lines`` "
        "count of state rows the parser could not structure (assert it is 0 so "
        "an unrecognised line is not a silent all-clear). No firewall change; "
        "reads the live state table (zero-change measurement). A failed read "
        "raises so a pinned Sensor evaluates ``unknown`` rather than a false "
        "all-clear. Same-subnet flows do not transit the pfSense and are "
        "invisible (see ``caveat``)."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "sanctioned_src": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "CIDR/host sources considered governed/sanctioned "
                    "(backplane egress, satellite runners, island services "
                    "VMs). A flow from one of these to a management port is "
                    "classified 'sanctioned'."
                ),
            },
            "mgmt_nets": {
                "type": "array",
                "items": _MGMT_NET_ITEM_SCHEMA,
                "description": (
                    "Destination management networks, each tagged with a leg "
                    "label. A state whose server-side (management-port) "
                    "endpoint falls in one of these CIDRs is counted under "
                    "that leg; states outside every CIDR are ignored."
                ),
            },
            "mgmt_ports": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Management-plane TCP ports. Defaults to the "
                    "443/22/902/5480 class when omitted."
                ),
            },
            "baseline_src": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional known/expected NON-sanctioned CIDR/host "
                    "sources that must NOT raise the unexpected-source signal "
                    "(e.g. the operator set captured at Sensor-creation time). "
                    "unexpected = non-sanctioned AND not in the union of "
                    "sanctioned_src and baseline_src."
                ),
            },
        },
        "required": ["sanctioned_src", "mgmt_nets"],
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "legs": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "sanctioned_states": {"type": "integer"},
                        "non_sanctioned_states": {"type": "integer"},
                        "coverage_pct": {"type": ["number", "null"]},
                    },
                },
            },
            "total_states_classified": {"type": "integer"},
            "unparsed_lines": {"type": "integer"},
            "sanctioned_state_count": {"type": "integer"},
            "non_sanctioned_state_count": {"type": "integer"},
            "sanctioned_source_count": {"type": "integer"},
            "non_sanctioned_source_count": {"type": "integer"},
            "unexpected_source_count": {"type": "integer"},
            "coverage_pct": {"type": ["number", "null"]},
            "non_sanctioned_sources": {"type": "array", "items": _SOURCE_ROW_SCHEMA},
            "non_sanctioned_sources_truncated": {"type": "boolean"},
            "unexpected_sources": {"type": "array", "items": _SOURCE_ROW_SCHEMA},
            "caveat": {"type": "string"},
        },
        "additionalProperties": True,
    },
    group_key="firewall",
    tags=("read-only", "firewall", "state-table", "governance", "pfsense"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": _WHEN_TO_USE_MGMT_FLOW,
        "parameter_hints": {
            "sanctioned_src": "Required. List of CIDR/host strings.",
            "mgmt_nets": "Required. List of {cidr, leg} objects.",
            "mgmt_ports": "Optional. Defaults to [443, 22, 902, 5480].",
            "baseline_src": "Optional. Known non-sanctioned sources to exempt.",
        },
        "output_shape": (
            "``{legs: {<leg>: {sanctioned_states, non_sanctioned_states, "
            "coverage_pct}}, total_states_classified, unparsed_lines, "
            "sanctioned_state_count, "
            "non_sanctioned_state_count, sanctioned_source_count, "
            "non_sanctioned_source_count, unexpected_source_count, "
            "coverage_pct, non_sanctioned_sources: [{src, leg, ports, "
            "states}], non_sanctioned_sources_truncated, unexpected_sources: "
            "[{src, leg, ports, states}], caveat}``. The ``*_source_count`` "
            "scalars are always exact; ``non_sanctioned_sources`` is capped "
            "while ``unexpected_sources`` is complete. ``unparsed_lines`` "
            "counts state rows the parser could not structure (assert it "
            "is 0 so an unrecognised state is not a silent all-clear)."
        ),
    },
)

#: The management-plane flow ops appended to :data:`PFSENSE_OPS`
#: (meho-internal#252). One op today; the tuple keeps the "import a module
#: tuple and splat it" registration shape.
MGMT_FLOW_OPS: tuple[PfSenseOp, ...] = (MGMT_FLOW_SUMMARY_OP,)
