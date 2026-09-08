# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structural read-side redaction of Kubernetes ``Secret`` payloads (#3501).

The connector-boundary Tier-1 engine (``redaction/``) matches only
**labelled** secret shapes inside string leaves (``password=…``,
``Bearer …``). A Kubernetes ``Secret`` object's payload is the opposite
shape: arbitrary, operator-chosen keys (``tls.key``, an app token, a
``.dockerconfigjson`` blob) whose base64 ``data`` values carry no in-leaf
label the pattern engine can key on. So a read that returns a ``Secret``
object would leak every value verbatim unless a value happened to match a
named credential pattern -- exactly the read-side payload gap
meho-internal#322 surfaced for the Envision visitor tenant.

This module is the k8s per-connector redactor (the same shape as
``connectors/keycloak/redaction.py`` and ``connectors/rabbitmq/redact.py``):
a pure, structural walk that recognises a ``Secret`` by its ``kind`` and
replaces **every** ``data`` / ``stringData`` value with a fixed
placeholder carrying a truncated SHA-256 digest -- regardless of whether
the value matches any pattern. Key names are preserved so a read still
answers "which keys does this Secret carry?"; the digest lets a caller
correlate two reads of the same value without ever seeing the bytes (the
same ``sha256:`` convention the redaction engine's ``hash`` action uses).

Where it runs
=============

Applied inside the connector's own read path, before the value reaches
the JSONFlux reducer / result handle, the audit row, or the broadcast
feed. Today the only shipped read that can surface a raw ``Secret`` is
the generic dynamic read ``k8s.cr.list`` / ``k8s.cr.info`` (pointable at
core Secrets with ``group=""``, ``version="v1"``, ``plural="secrets"``)
-- wired through ``ops_customresource.custom_resource_row``. A future
single-object / list Secret read reuses this same helper.

Note on the current CR projection: ``custom_resource_row`` otherwise
keeps only ``metadata`` + a bounded ``.spec`` excerpt and drops every
other top-level field, and a ``Secret`` has no ``.spec`` -- so before
this module a CR read of a ``Secret`` returned no ``data`` at all. This
redactor lets the projection surface the ``data`` / ``stringData`` **key
inventory** (values redacted) instead, which is both safe and more
useful than silently dropping it. ``k8s.secret.read_to_ref`` (#3496) is
unaffected: it returns only a Vault ``secret_ref`` + a SHA-256, never a
``data`` map, so there is nothing here for this walk to match.
"""

from __future__ import annotations

import hashlib
from typing import Any

__all__ = [
    "REDACTED_PREFIX",
    "SECRET_DATA_FIELDS",
    "SECRET_KIND",
    "redact_kubernetes_payload",
    "redact_secret_value",
]

#: The ``kind`` field value that marks a Kubernetes ``Secret`` object.
#: Matched case-sensitively -- the API server always emits the canonical
#: ``"Secret"`` casing, and a case-insensitive match risks colliding with
#: an unrelated custom resource whose kind merely spells the word.
SECRET_KIND = "Secret"

#: The two maps on a ``Secret`` whose values are secret material:
#: ``data`` (base64-encoded) and ``stringData`` (write-only plaintext,
#: which the API server normally folds into ``data`` but which a read of
#: an in-flight apply could still surface).
SECRET_DATA_FIELDS: tuple[str, ...] = ("data", "stringData")

#: Fixed marker written in place of a Secret value. Non-empty (rather
#: than dropping the key) so a caller sees a value *existed*; carries the
#: digest so two reads of the same value are correlatable.
REDACTED_PREFIX = "[REDACTED:k8s-secret"

#: Hex characters of the SHA-256 kept in the placeholder -- mirrors the
#: redaction engine's ``hash`` action (``sha256:<first-12-hex>``).
_DIGEST_HEX_CHARS = 12


def redact_secret_value(value: Any) -> str:
    """Return the fixed placeholder + truncated SHA-256 digest for *value*.

    The digest is taken over the value **as stored** -- base64 text for a
    ``data`` entry, plaintext for a ``stringData`` entry -- so it is
    stable across reads of the same Secret. A non-string value (a
    malformed Secret) is coerced with ``str`` before hashing so the walk
    never raises on unexpected shapes.
    """
    encoded = (value if isinstance(value, str) else str(value)).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:_DIGEST_HEX_CHARS]
    return f"{REDACTED_PREFIX};sha256:{digest}]"


def redact_kubernetes_payload(value: Any) -> Any:
    """Return *value* with every ``kind: Secret`` object's data redacted.

    Recursively walks dicts and lists. A mapping is treated as a
    Kubernetes ``Secret`` when its ``kind`` equals :data:`SECRET_KIND`;
    each value of its ``data`` / ``stringData`` maps is replaced via
    :func:`redact_secret_value` (key names kept). Every other value is
    walked so a ``Secret`` nested inside a ``List``-kind envelope or a
    ``k8s.cr.list`` ``items`` array is still caught. Scalars pass through
    unchanged. The input is never mutated -- a new structure is returned,
    so a half-redacted object can never be left aliased elsewhere.
    """
    if isinstance(value, dict):
        is_secret = value.get("kind") == SECRET_KIND
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret and key in SECRET_DATA_FIELDS and isinstance(item, dict):
                out[key] = {
                    inner_key: redact_secret_value(inner_val)
                    for inner_key, inner_val in item.items()
                }
            else:
                out[key] = redact_kubernetes_payload(item)
        return out
    if isinstance(value, list):
        return [redact_kubernetes_payload(item) for item in value]
    return value
