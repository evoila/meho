"""Trust model enforcement for connector operations.

Three trust tiers:
  READ:        Auto-execute (no confirmation needed)
  WRITE:       Requires --confirmed flag
  DESTRUCTIVE: Requires --confirm with matching text
"""

from __future__ import annotations


def _build_confirm_text(operation: dict, params: dict) -> str:
    """Build confirmation text from operation name and first significant param.

    The confirm text is what the user must type exactly to confirm a
    DESTRUCTIVE operation.

    Args:
        operation: Operation dict with display_name.
        params: Operation parameters.

    Returns:
        Confirmation text string.
    """
    display_name = operation.get("display_name", "unknown")

    # Find the first significant param value
    significant_value = None
    for key, value in params.items():
        if value and str(value).strip():
            significant_value = str(value)
            break

    if significant_value:
        return f"{display_name} {significant_value}"
    return display_name


def enforce_trust(
    operation: dict,
    params: dict,
    confirmed: bool = False,
    confirm_text: str | None = None,
) -> dict | None:
    """Enforce trust model for an operation.

    Args:
        operation: Operation dict with trust_tier, display_name, connector_name, description.
        params: Operation parameters.
        confirmed: Whether --confirmed flag was passed (for WRITE operations).
        confirm_text: Typed confirmation text (for DESTRUCTIVE operations).

    Returns:
        None if the operation is allowed to proceed.
        Dict with confirmation requirements if the operation is blocked.
    """
    trust_tier = operation.get("trust_tier", "READ")
    display_name = operation.get("display_name", "unknown")
    connector_name = operation.get("connector_name", "unknown")
    description = operation.get("description", "")

    if trust_tier == "READ":
        return None

    if trust_tier == "WRITE":
        if confirmed:
            return None
        return {
            "status": "confirmation_required",
            "operation": display_name,
            "connector": connector_name,
            "params": params,
            "impact": f"WRITE operation: {description}",
            "hint": "Re-run with --confirmed to execute",
        }

    if trust_tier == "DESTRUCTIVE":
        expected = _build_confirm_text(operation, params)
        if confirm_text is not None and confirm_text == expected:
            return None
        return {
            "status": "destructive_confirmation",
            "operation": display_name,
            "connector": connector_name,
            "confirm_text": expected,
            "hint": f'Re-run with --confirm "{expected}" to execute',
        }

    # Unknown trust tier -- treat as WRITE
    if confirmed:
        return None
    return {
        "status": "confirmation_required",
        "operation": display_name,
        "connector": connector_name,
        "params": params,
        "impact": f"{trust_tier} operation: {description}",
        "hint": "Re-run with --confirmed to execute",
    }
