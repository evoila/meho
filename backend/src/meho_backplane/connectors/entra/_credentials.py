# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The client secrets and certificates of apps: what MEHO reads and what it never shows.

An app registration or an enterprise app (service principal) can hold client
secrets (``passwordCredentials``) and certificates (``keyCredentials``).
Graph never returns a secret's value (``secretText`` is only in the answer
that makes the secret), but it does return ``hint``, the first characters of
each secret, and for some reads the certificate data (``key``).

MEHO reads only the end dates. :func:`summarize` counts the credentials, the
ones that have run out, and finds the next end date. The rows of
``entra.app.list`` and ``entra.service_principal.list`` hold only these
numbers and dates, never a credential itself. :func:`scrub_credentials` cuts
the credentials in a raw Graph answer down to their id, name and dates before
the answer reaches a trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "CredentialSummary",
    "credential_fields",
    "parse_graph_time",
    "scrub_credentials",
    "summarize",
    "utc_text",
]

#: The fields of a credential that may reach a trace. ``hint``, ``secretText``,
#: ``key`` and ``customKeyIdentifier`` never do.
_SAFE_CREDENTIAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"keyId", "displayName", "startDateTime", "endDateTime", "type", "usage"}
)

#: The two credential lists of an app or a service principal.
_CREDENTIAL_LISTS: Final[frozenset[str]] = frozenset({"passwordCredentials", "keyCredentials"})


@dataclass(frozen=True)
class CredentialSummary:
    """What MEHO keeps of one credential list: counts and the next end date."""

    count: int
    expired: int
    next_end: datetime | None


def parse_graph_time(value: object) -> datetime | None:
    """Read an ISO 8601 time from Graph (always UTC) as an aware ``datetime``, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_text(value: datetime | None) -> str | None:
    """Write *value* as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), or ``None``."""
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(credentials: object, now: datetime) -> CredentialSummary:
    """Count the credentials, those that have run out, and find the next end date.

    Only ``endDateTime`` is read. A credential whose end date is *now* or
    earlier has run out. ``next_end`` is the earliest end date after *now*,
    or ``None`` when no credential is still valid. A credential without a
    readable end date counts, but neither as run out nor as next.
    """
    items = (
        [item for item in credentials if isinstance(item, dict)]
        if isinstance(credentials, list)
        else []
    )
    ends = [parse_graph_time(item.get("endDateTime")) for item in items]
    expired = sum(1 for end in ends if end is not None and end <= now)
    upcoming = [end for end in ends if end is not None and end > now]
    return CredentialSummary(
        count=len(items), expired=expired, next_end=min(upcoming, default=None)
    )


def credential_fields(
    secrets: CredentialSummary, certificates: CredentialSummary
) -> dict[str, Any]:
    """The credential columns of an app row: counts and next end dates, nothing else."""
    return {
        "secret_count": secrets.count,
        "expired_secret_count": secrets.expired,
        "next_secret_expiry": utc_text(secrets.next_end),
        "certificate_count": certificates.count,
        "expired_certificate_count": certificates.expired,
        "next_certificate_expiry": utc_text(certificates.next_end),
    }


def scrub_credentials(value: Any) -> Any:
    """Return *value* with every app credential cut down to its safe fields.

    Walks a Graph answer and replaces each item of a ``passwordCredentials``
    or ``keyCredentials`` list with only its id, name and dates (and the key
    type and usage). Everything else is returned as it is.
    """
    if isinstance(value, list):
        return [scrub_credentials(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, child in value.items():
        if key in _CREDENTIAL_LISTS and isinstance(child, list):
            cleaned[key] = [
                {name: field for name, field in item.items() if name in _SAFE_CREDENTIAL_FIELDS}
                if isinstance(item, dict)
                else None
                for item in child
            ]
        else:
            cleaned[key] = scrub_credentials(child)
    return cleaned
