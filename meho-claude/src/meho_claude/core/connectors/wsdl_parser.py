"""WSDL parsing and operation extraction.

Parses WSDL service definitions into a list of Operation models.
Mirrors openapi_parser.py -- auto-discovers all operations across all
services/ports/bindings with trust tier heuristics.
"""

from __future__ import annotations

from zeep import Client, Settings

from meho_claude.core.connectors.models import Operation

# Trust tier heuristics for SOAP operation names
_SOAP_TRUST_HEURISTICS: dict[str, list[str]] = {
    "READ": ["Get", "List", "Find", "Search", "Query", "Retrieve", "Fetch", "Read"],
    "WRITE": ["Create", "Update", "Set", "Add", "Modify", "Insert", "Put"],
    "DESTRUCTIVE": ["Delete", "Remove", "Destroy", "Purge", "Drop", "Clear"],
}


def _infer_trust_tier(op_name: str) -> str:
    """Infer trust tier from SOAP operation name prefix.

    Uses case-insensitive prefix matching. Returns "WRITE" as safe default
    for unrecognized operation names.
    """
    op_lower = op_name.lower()
    for tier, prefixes in _SOAP_TRUST_HEURISTICS.items():
        for prefix in prefixes:
            if op_lower.startswith(prefix.lower()):
                return tier
    return "WRITE"


def _extract_input_schema(operation: object) -> dict:
    """Extract parameter names from a WSDL operation's input body element.

    Returns a dict with "properties" containing parameter names/types.
    On failure, returns empty dict (defensive -- many WSDLs have complex types).
    """
    try:
        input_msg = operation.input  # type: ignore[union-attr]
        if not hasattr(input_msg, "body") or input_msg.body is None:
            return {}

        element = input_msg.body
        if not hasattr(element, "type") or element.type is None:
            return {}

        # Try to extract element properties from the XSD type
        xsd_type = element.type
        if hasattr(xsd_type, "elements"):
            properties = {}
            for elem_name, elem_obj in xsd_type.elements:
                properties[elem_name] = {"type": "string"}
            if properties:
                return {"properties": properties}
    except Exception:
        pass

    return {}


def parse_wsdl(wsdl_source: str, connector_name: str) -> list[Operation]:
    """Parse a WSDL document into a list of Operation models.

    Args:
        wsdl_source: URL (http/https) or local file path to the WSDL.
        connector_name: Name of the connector these operations belong to.

    Returns:
        List of Operation models extracted from all services/ports/bindings.
    """
    client = Client(wsdl_source, settings=Settings(strict=False))
    operations: list[Operation] = []

    for service_name, service in client.wsdl.services.items():
        for port_name, port in service.ports.items():
            for op_name, op in port.binding._operations.items():
                trust_tier = _infer_trust_tier(op_name)
                input_schema = _extract_input_schema(op)

                operations.append(
                    Operation(
                        connector_name=connector_name,
                        operation_id=f"{service_name}.{op_name}",
                        display_name=op_name,
                        description=f"{service_name}.{port_name}.{op_name}",
                        trust_tier=trust_tier,
                        input_schema=input_schema,
                        tags=[service_name, "soap"],
                    )
                )

    # Discard parsed WSDL immediately (memory management per openapi_parser.py pattern)
    del client

    return operations
