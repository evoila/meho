# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tempo Response Serializers.

Transform raw Tempo API responses (OTLP JSON) into clean, structured data
for the agent. Handles trace search results, full traces with span flattening,
span detail progressive disclosure, service graph, and trace-derived metrics.
"""

from datetime import UTC, datetime
from typing import Any

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _ns_to_ms(nano_str: str) -> float:
    """Convert nanosecond string to milliseconds."""
    try:
        return int(nano_str) / 1_000_000
    except (ValueError, TypeError):
        return 0.0


def _ns_to_iso(nano_str: str) -> str:
    """Convert nanosecond string to ISO8601."""
    try:
        ns = int(nano_str)
        seconds = ns / 1_000_000_000
        dt = datetime.fromtimestamp(seconds, tz=UTC)
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        return str(nano_str)


def _extract_attribute(
    attrs: list[dict[str, Any]], key: str
) -> Any:  # NOSONAR (cognitive complexity)
    """
    Find attribute value by key in OTLP attribute list format.

    OTLP attributes are structured as:
    [{"key": "http.method", "value": {"stringValue": "GET"}}, ...]

    Supports stringValue, intValue, boolValue, doubleValue, arrayValue.
    """
    for attr in attrs:
        if attr.get("key") == key:
            value = attr.get("value", {})
            if "stringValue" in value:
                return value["stringValue"]
            if "intValue" in value:
                return value["intValue"]
            if "boolValue" in value:
                return value["boolValue"]
            if "doubleValue" in value:
                return value["doubleValue"]
            if "arrayValue" in value:
                array_values = value["arrayValue"].get("values", [])
                return [_extract_single_value(v) for v in array_values]
            if "kvlistValue" in value:
                return {
                    pair["key"]: _extract_single_value(pair.get("value", {}))
                    for pair in value["kvlistValue"].get("values", [])
                }
            return value
    return None


def _extract_single_value(value: dict[str, Any]) -> Any:
    """Extract a single value from an OTLP value object."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return value["intValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    return value


def _parse_otlp_spans(data: dict) -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """
    Walk resourceSpans -> scopeSpans -> spans, extracting service.name
    from resource attributes.

    Returns flat list of (service_name, span, resource_attributes) tuples.
    """
    results: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []

    # Handle both direct data and nested under "batches" or "resourceSpans"
    resource_spans = data.get("resourceSpans", data.get("batches", []))

    for rs in resource_spans:
        resource = rs.get("resource", {})
        resource_attrs = resource.get("attributes", [])
        service_name = _extract_attribute(resource_attrs, "service.name") or "unknown"

        scope_spans = rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", []))
        for ss in scope_spans:
            spans = ss.get("spans", [])
            for span in spans:
                results.append((str(service_name), span, resource_attrs))

    return results


def _get_span_status(span: dict[str, Any]) -> str:
    """Extract span status code as human-readable string."""
    status = span.get("status", {})
    code = status.get("code", 0)
    # OTLP status codes: 0=UNSET, 1=OK, 2=ERROR
    if code == 2:
        return "error"
    if code == 1:
        return "ok"
    return "unset"


def _compute_duration_ms(span: dict[str, Any]) -> float:
    """Compute span duration in milliseconds from start/end nanoseconds."""
    start_ns = span.get("startTimeUnixNano", "0")
    end_ns = span.get("endTimeUnixNano", "0")
    try:
        return (int(end_ns) - int(start_ns)) / 1_000_000
    except (ValueError, TypeError):
        return 0.0


# =============================================================================
# SERIALIZERS
# =============================================================================


def serialize_trace_search(data: dict, limit: int = 20) -> dict:  # NOSONAR (cognitive complexity)
    """
    Transform Tempo search API response into compact trace summaries.

    Input: Tempo search API response (list of trace summaries under "traces" key).
    Output: {
        "summary": {total_traces, time_range, showing, truncated},
        "traces": [{trace_id, root_service, root_operation, duration_ms,
                     span_count, error_count}, ...]
    }
    """
    traces_raw = data.get("traces", [])
    total_traces = len(traces_raw)
    truncated = total_traces > limit
    showing = min(total_traces, limit)

    traces = []
    for trace in traces_raw[:limit]:
        trace_id = trace.get("traceID", trace.get("traceId", ""))
        root_service = trace.get("rootServiceName", "unknown")
        root_operation = trace.get("rootTraceName", "unknown")
        duration_ms = trace.get("durationMs", 0)

        # Some Tempo versions return duration in nanoseconds
        if "durationMs" not in trace and "duration" in trace:
            try:
                duration_ms = int(trace["duration"]) / 1_000_000
            except (ValueError, TypeError):
                duration_ms = 0

        span_count = trace.get(
            "spanCount",
            trace.get("spanSets", [{}])[0].get("matched", 0) if trace.get("spanSets") else 0,
        )
        error_count = trace.get("errorCount", 0)

        # Count errors from spanSets if available
        if error_count == 0 and trace.get("spanSets"):
            for span_set in trace.get("spanSets", []):
                for span in span_set.get("spans", []):
                    attrs = span.get("attributes", [])
                    status = _extract_attribute(attrs, "status")
                    if status and str(status).lower() == "error":
                        error_count += 1

        traces.append(
            {
                "trace_id": trace_id,
                "root_service": root_service,
                "root_operation": root_operation,
                "duration_ms": round(duration_ms, 1)
                if isinstance(duration_ms, float)
                else duration_ms,
                "span_count": span_count,
                "error_count": error_count,
            }
        )

    summary: dict[str, Any] = {
        "total_traces": total_traces,
        "showing": showing,
        "truncated": truncated,
    }

    return {"summary": summary, "traces": traces}


def serialize_full_trace(data: dict) -> dict:  # NOSONAR (cognitive complexity)
    """
    Transform OTLP JSON from /api/traces/{id} into flat span table.

    Flattens the nested OTLP structure (resourceSpans -> scopeSpans -> spans)
    into a flat table. Top 50 spans by duration if trace has >50 spans.
    Includes HTTP attributes when present in the core set.

    Output: {
        "summary": {trace_id, total_spans, showing, services, duration_ms},
        "spans": [{timestamp, service, operation, duration_ms, status,
                   span_id, parent_span_id, http_method?, http_url?,
                   http_status_code?}, ...]
    }
    """
    parsed = _parse_otlp_spans(data)

    # Build flat span list
    flat_spans: list[dict[str, Any]] = []
    services: set = set()
    trace_id = ""

    for service_name, span, _resource_attrs in parsed:
        services.add(service_name)
        span_id = span.get("spanId", "")
        parent_span_id = span.get("parentSpanId", "")

        if not trace_id:
            trace_id = span.get("traceId", "")

        duration_ms = _compute_duration_ms(span)
        status = _get_span_status(span)
        start_ns = span.get("startTimeUnixNano", "0")
        timestamp = _ns_to_iso(start_ns)
        operation = span.get("name", "unknown")

        span_entry: dict[str, Any] = {
            "timestamp": timestamp,
            "service": service_name,
            "operation": operation,
            "duration_ms": round(duration_ms, 2),
            "status": status,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
        }

        # Include core HTTP attributes when present
        attrs = span.get("attributes", [])
        http_method = _extract_attribute(attrs, "http.method") or _extract_attribute(
            attrs, "http.request.method"
        )
        http_url = _extract_attribute(attrs, "http.url") or _extract_attribute(attrs, "url.full")
        http_status_code = _extract_attribute(attrs, "http.status_code") or _extract_attribute(
            attrs, "http.response.status_code"
        )

        if http_method:
            span_entry["http_method"] = str(http_method)
        if http_url:
            span_entry["http_url"] = str(http_url)
        if http_status_code:
            span_entry["http_status_code"] = (
                int(http_status_code) if str(http_status_code).isdigit() else http_status_code
            )

        flat_spans.append(span_entry)

    total_spans = len(flat_spans)

    # Sort by duration descending and take top 50 if many spans
    flat_spans.sort(key=lambda s: s["duration_ms"], reverse=True)
    showing = min(total_spans, 50)
    display_spans = flat_spans[:50]

    # Compute total trace duration from min start to max end
    trace_duration_ms = 0.0
    if parsed:
        all_starts = []
        all_ends = []
        for _, span, _ in parsed:
            try:
                all_starts.append(int(span.get("startTimeUnixNano", "0")))
                all_ends.append(int(span.get("endTimeUnixNano", "0")))
            except (ValueError, TypeError):
                pass
        if all_starts and all_ends:
            trace_duration_ms = (max(all_ends) - min(all_starts)) / 1_000_000

    summary = {
        "trace_id": trace_id,
        "total_spans": total_spans,
        "showing": showing,
        "services": sorted(services),
        "duration_ms": round(trace_duration_ms, 2),
    }

    return {"summary": summary, "spans": display_spans}


def serialize_span_details(
    span_data: dict[str, Any], resource_attrs: list[dict[str, Any]]
) -> dict:  # NOSONAR (cognitive complexity)
    """
    Full unredacted span details for progressive disclosure deep-dive.

    No redaction, no truncation -- this is the deep-dive view.
    Includes ALL attributes, events (including exception stacktraces),
    links, and resource attributes.

    Output: Full span detail dict with all available data.
    """
    span_id = span_data.get("spanId", "")
    trace_id = span_data.get("traceId", "")
    parent_span_id = span_data.get("parentSpanId", "")
    operation = span_data.get("name", "unknown")
    kind_code = span_data.get("kind", 0)

    # Map span kind codes to readable strings
    kind_map = {
        0: "UNSPECIFIED",
        1: "INTERNAL",
        2: "SERVER",
        3: "CLIENT",
        4: "PRODUCER",
        5: "CONSUMER",
    }
    kind = kind_map.get(kind_code, f"UNKNOWN({kind_code})")

    duration_ms = _compute_duration_ms(span_data)
    status = _get_span_status(span_data)
    status_message = span_data.get("status", {}).get("message", "")

    start_ns = span_data.get("startTimeUnixNano", "0")
    end_ns = span_data.get("endTimeUnixNano", "0")

    # Extract ALL span attributes
    span_attrs = span_data.get("attributes", [])
    attributes: dict[str, Any] = {}
    for attr in span_attrs:
        key = attr.get("key", "")
        value = _extract_attribute([attr], key)
        if key:
            attributes[key] = value

    # Extract events (exception stacktraces, log entries, etc.)
    events: list[dict[str, Any]] = []
    for event in span_data.get("events", []):
        event_entry: dict[str, Any] = {
            "name": event.get("name", ""),
            "timestamp": _ns_to_iso(str(event.get("timeUnixNano", "0"))),
        }
        event_attrs: dict[str, Any] = {}
        for attr in event.get("attributes", []):
            key = attr.get("key", "")
            value = _extract_attribute([attr], key)
            if key:
                event_attrs[key] = value
        if event_attrs:
            event_entry["attributes"] = event_attrs
        events.append(event_entry)

    # Extract links
    links: list[dict[str, Any]] = []
    for link in span_data.get("links", []):
        link_entry: dict[str, Any] = {
            "trace_id": link.get("traceId", ""),
            "span_id": link.get("spanId", ""),
        }
        link_attrs: dict[str, Any] = {}
        for attr in link.get("attributes", []):
            key = attr.get("key", "")
            value = _extract_attribute([attr], key)
            if key:
                link_attrs[key] = value
        if link_attrs:
            link_entry["attributes"] = link_attrs
        links.append(link_entry)

    # Extract resource attributes
    resource: dict[str, Any] = {}
    for attr in resource_attrs:
        key = attr.get("key", "")
        value = _extract_attribute([attr], key)
        if key:
            resource[key] = value

    result: dict[str, Any] = {
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "operation": operation,
        "kind": kind,
        "start_time": _ns_to_iso(start_ns),
        "end_time": _ns_to_iso(end_ns),
        "duration_ms": round(duration_ms, 2),
        "status": status,
        "attributes": attributes,
        "resource": resource,
    }

    if status_message:
        result["status_message"] = status_message
    if events:
        result["events"] = events
    if links:
        result["links"] = links

    return result


def serialize_service_graph(data: dict) -> dict:
    """
    Transform Tempo service graph response into nodes + edges tables.

    Handles empty/404 gracefully (metrics-generator not enabled).

    Output: {
        "summary": {total_services, total_edges, highest_error_rate_service,
                     highest_latency_edge},
        "nodes": [{service_name, span_count, error_rate, avg_duration_ms}, ...],
        "edges": [{source, target, call_rate, error_rate, p50_ms, p95_ms}, ...]
    }
    """
    if not data:
        return {
            "summary": {
                "total_services": 0,
                "total_edges": 0,
                "note": "Service graph unavailable. Tempo metrics-generator may not be enabled.",
            },
            "nodes": [],
            "edges": [],
        }

    # Parse nodes from the service graph response
    # Tempo service graph format varies -- handle common formats
    nodes_raw = data.get("nodes", data.get("services", []))
    edges_raw = data.get("edges", data.get("connections", []))

    nodes: list[dict[str, Any]] = []
    highest_error_service: str | None = None
    highest_error_rate: float = 0.0

    for node in nodes_raw:
        service_name = node.get("service_name", node.get("name", node.get("id", "unknown")))
        span_count = node.get("span_count", node.get("total", 0))
        error_rate = node.get("error_rate", node.get("errors", 0))
        avg_duration_ms = node.get("avg_duration_ms", node.get("avg_latency", 0))

        # Normalize error_rate to float
        if isinstance(error_rate, int) and span_count > 0:
            error_rate = error_rate / span_count
        error_rate = round(float(error_rate), 4)

        nodes.append(
            {
                "service_name": service_name,
                "span_count": span_count,
                "error_rate": error_rate,
                "avg_duration_ms": round(float(avg_duration_ms), 2),
            }
        )

        if error_rate > highest_error_rate:
            highest_error_rate = error_rate
            highest_error_service = service_name

    edges: list[dict[str, Any]] = []
    highest_latency_edge: str | None = None
    highest_latency: float = 0.0

    for edge in edges_raw:
        source = edge.get("source", edge.get("from", "unknown"))
        target = edge.get("target", edge.get("to", "unknown"))
        call_rate = edge.get("call_rate", edge.get("rate", 0))
        error_rate = edge.get("error_rate", edge.get("errors", 0))
        p50_ms = edge.get("p50_ms", edge.get("p50", 0))
        p95_ms = edge.get("p95_ms", edge.get("p95", 0))

        edges.append(
            {
                "source": source,
                "target": target,
                "call_rate": round(float(call_rate), 2),
                "error_rate": round(float(error_rate), 4),
                "p50_ms": round(float(p50_ms), 2),
                "p95_ms": round(float(p95_ms), 2),
            }
        )

        if float(p95_ms) > highest_latency:
            highest_latency = float(p95_ms)
            highest_latency_edge = f"{source} -> {target}"

    summary: dict[str, Any] = {
        "total_services": len(nodes),
        "total_edges": len(edges),
    }
    if highest_error_service:
        summary["highest_error_rate_service"] = highest_error_service
    if highest_latency_edge:
        summary["highest_latency_edge"] = highest_latency_edge

    return {"summary": summary, "nodes": nodes, "edges": edges}


def serialize_trace_metrics(aggregated: dict[str, dict[str, Any]]) -> dict:
    """
    Transform aggregated per-service trace data into metrics table.

    Input: Dict mapping service_name -> {
        "span_count": int, "error_count": int,
        "durations": [float, ...]
    }

    Output: {
        "services": [{service_name, span_count, error_count,
                      avg_duration_ms, p95_duration_ms}, ...]
    }
    """
    services: list[dict[str, Any]] = []

    for service_name, stats in sorted(aggregated.items()):
        span_count = stats.get("span_count", 0)
        error_count = stats.get("error_count", 0)
        durations = stats.get("durations", [])

        avg_duration = sum(durations) / len(durations) if durations else 0.0
        p95_duration = _percentile(durations, 95) if durations else 0.0

        services.append(
            {
                "service_name": service_name,
                "span_count": span_count,
                "error_count": error_count,
                "avg_duration_ms": round(avg_duration, 2),
                "p95_duration_ms": round(p95_duration, 2),
            }
        )

    return {"services": services}


def _percentile(values: list[float], percentile: int) -> float:
    """Calculate percentile from a list of values."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = (percentile / 100) * (len(sorted_vals) - 1)
    lower = int(idx)
    upper = lower + 1
    if upper >= len(sorted_vals):
        return sorted_vals[-1]
    weight = idx - lower
    return sorted_vals[lower] * (1 - weight) + sorted_vals[upper] * weight
