"""Audit log writer with secret redaction.

Appends JSON-line entries to ~/.meho/audit.log for every connector call.
Secrets are redacted before writing. The log is append-only and greppable.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# Keys whose values should be redacted (case-insensitive match)
_SECRET_KEYS = frozenset({"password", "token", "secret", "api_key", "apikey", "authorization"})
_REDACTED = "***REDACTED***"


def _redact_secrets(params: dict) -> dict:
    """Replace values for known secret keys with a redaction marker.

    Case-insensitive key matching. Recurses into nested dicts.

    Args:
        params: Dict of parameters to redact.

    Returns:
        New dict with secret values replaced by "***REDACTED***".
    """
    result = {}
    for key, value in params.items():
        if key.lower() in _SECRET_KEYS:
            result[key] = _REDACTED
        elif isinstance(value, dict):
            result[key] = _redact_secrets(value)
        else:
            result[key] = value
    return result


def audit_log(
    log_path: Path,
    connector: str,
    operation: str,
    trust_tier: str,
    params: dict,
    result_status: str,
) -> None:
    """Append a single audit log entry as a JSON line.

    Args:
        log_path: Path to the audit log file.
        connector: Connector name.
        operation: Operation ID.
        trust_tier: Trust tier (READ, WRITE, DESTRUCTIVE).
        params: Operation parameters (will be redacted).
        result_status: Result status string (e.g., "success", "error").
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "connector": connector,
        "operation": operation,
        "trust_tier": trust_tier,
        "params": _redact_secrets(params),
        "result": result_status,
    }

    line = json.dumps(entry, separators=(",", ":"))

    with open(log_path, "a") as f:
        f.write(line + "\n")
