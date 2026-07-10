# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Default credential redaction for RabbitMQ Management payloads.

The load-bearing safety nuance of the read-only RabbitMQ connector
(#2233). Several Management HTTP API surfaces echo back the credentials
an operator stored when configuring the broker:

* ``/api/parameters/shovel`` (dynamic shovels) and the shovel entries in
  ``/api/definitions`` carry ``src-uri`` / ``dest-uri`` values shaped
  ``amqp://user:pass@host`` — the AMQP URI embeds the upstream broker's
  username and password in the userinfo component.
* ``/api/federation-links`` and federation-upstream parameters carry the
  same ``uri`` shape.
* ``/api/definitions`` additionally exports every user's
  ``password_hash`` and can carry ``password`` fields for imported users.

Surfacing those verbatim to an agent (or into an ``OperationResult`` that
lands in the audit log / a chat transcript) would leak upstream broker
credentials. :func:`redact_rabbitmq_payload` walks the decoded JSON and
returns a redacted copy so the result is safe to surface by default:

1. Any ``amqp://`` / ``amqps://`` URI has its ``user:pass@`` userinfo
   replaced with ``***@`` — the host, port, vhost, and query params are
   preserved so the topology is still legible.
2. Any mapping key whose name contains ``password`` or ``secret``
   (case-insensitive — catches ``password``, ``password_hash``, and a
   vendor's ``client_secret``) has its value replaced with ``***``,
   regardless of the value's type.

The walk is purely structural and returns a **new** value — the input is
never mutated, so a caller that also needs the raw payload (there is none
today, but the connector's audit trail records the pre-redaction view via
the dispatcher's own middleware) is unaffected.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REDACTED", "redact_rabbitmq_payload"]

#: The placeholder substituted for a redacted value or URI userinfo.
REDACTED = "***"

#: Matches the ``scheme://userinfo@`` prefix of an AMQP(S) URI. The
#: userinfo run is everything up to the first ``@`` that is not itself a
#: ``/`` (so a credential-free ``amqp://host/vhost`` never matches — it
#: has no ``@`` before the path). The capture keeps the scheme so the
#: substitution can splice the ``***@`` sentinel back in.
_AMQP_USERINFO_RE: re.Pattern[str] = re.compile(r"(amqps?://)[^/@\s]*@")


def _redact_uri_userinfo(value: str) -> str:
    """Blank the userinfo of any ``amqp(s)://user:pass@host`` URI in *value*.

    Operates on the whole string (a field may embed a URI mid-text, and
    ``re.sub`` rewrites every occurrence) so both a bare ``src-uri`` value
    and a URI nested in a larger connection string are covered. A URI with
    no userinfo is returned unchanged — the pattern requires an ``@``.
    """
    return _AMQP_USERINFO_RE.sub(rf"\1{REDACTED}@", value)


#: Mapping keys whose value must be blanked wholesale. Substring match
#: (case-insensitive) so ``password``, ``password_hash``, ``secret``, and
#: ``client_secret`` are all caught without enumerating every variant.
_SENSITIVE_KEY_RE: re.Pattern[str] = re.compile(r"password|secret", re.IGNORECASE)


def redact_rabbitmq_payload(payload: Any) -> Any:
    """Return a redacted deep copy of a decoded RabbitMQ Management payload.

    Recurses through mappings and sequences:

    * a mapping key matching :data:`_SENSITIVE_KEY_RE` → its value becomes
      :data:`REDACTED` (the sub-tree is not recursed into — the whole
      value is dropped);
    * a string leaf → AMQP URI userinfo is blanked via
      :func:`_redact_uri_userinfo`;
    * every other scalar is returned unchanged.

    Lists and tuples are walked element-wise; the return preserves list
    shape (a tuple decodes as a list from JSON, so the output is always a
    list for sequence input). The input object is never mutated.
    """
    if isinstance(payload, dict):
        redacted: dict[Any, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_rabbitmq_payload(value)
        return redacted
    if isinstance(payload, (list, tuple)):
        return [redact_rabbitmq_payload(item) for item in payload]
    if isinstance(payload, str):
        return _redact_uri_userinfo(payload)
    return payload
