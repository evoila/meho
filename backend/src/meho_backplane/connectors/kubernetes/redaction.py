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
feed. It is wired into ``ops_customresource.custom_resource_row``, which
``k8s.cr.list`` / ``k8s.cr.info`` share, for any object whose ``kind`` is
``Secret``. Those two ops cannot read core Secrets: their ``group``
param must not be empty, and the custom-object API builds
``/apis/{group}/...`` paths, not the core ``/api/v1`` path. The way to
look at core Secrets is ``k8s.secret.list`` / ``k8s.secret.info``
(``ops_secret_list.py``). They build their answer from names only and
run this walk over it as a second net.

The flight recorder's Kubernetes transport hook (``_wire_span.py``,
#3841) runs the same walk over every JSON request and response body
before the body reaches the recorder, so a ``k8s.apply`` of a Secret or
a ``k8s.cr.*`` read of one never records a value either. On a Secret
the walk also replaces every ``metadata.annotations`` value (keys kept),
because deploy tools can copy the whole manifest into an annotation.

The walk also masks the values of a ``kind: HelmChart`` object (the k3s /
RKE2 ``helm.cattle.io/v1`` resource, and the items of a ``HelmChartList``).
Its ``spec.valuesContent`` text and every value of its ``spec.set`` map
are the Helm values of an install, which often hold passwords. Each is
replaced by the same placeholder plus digest; the ``set`` keys stay. A
``HelmChart`` made with ``kubectl apply`` also carries its whole manifest,
values included, in the last-applied annotation, so every
``metadata.annotations`` value of a chart is replaced too (keys kept),
as on a Secret. So a ``k8s.meho.release.info`` read of the MEHO chart, or
a ``k8s.cr.info`` of any ``HelmChart``, records and returns no values text.

Note on the current CR projection: ``custom_resource_row`` otherwise
keeps only ``metadata`` + bounded ``.spec`` / ``.status`` excerpts and
drops every other top-level field, and a ``Secret`` has no ``.spec`` --
so before this module a CR read of a ``Secret`` returned no ``data`` at
all. This redactor lets the projection surface the ``data`` /
``stringData`` **key inventory** (values redacted) instead, which is
both safe and more useful than silently dropping it. ``k8s.secret.read_to_ref`` (#3496) is
unaffected: it returns only a Vault ``secret_ref`` + a SHA-256, never a
``data`` map, so there is nothing here for this walk to match.
"""

from __future__ import annotations

import hashlib
from typing import Any

__all__ = [
    "HELM_CHART_KIND",
    "HELM_CHART_LIST_KIND",
    "HELM_CHART_VALUE_FIELDS",
    "LAST_APPLIED_ANNOTATION",
    "REDACTED_PREFIX",
    "SECRET_DATA_FIELDS",
    "SECRET_KIND",
    "SECRET_LIST_KIND",
    "redact_kubernetes_payload",
    "redact_secret_value",
]

#: The ``kind`` field value that marks a Kubernetes ``Secret`` object.
#: Matched case-sensitively -- the API server always emits the canonical
#: ``"Secret"`` casing, and a case-insensitive match risks colliding with
#: an unrelated custom resource whose kind merely spells the word.
SECRET_KIND = "Secret"

#: The ``kind`` of the envelope the API server returns for a Secret list
#: (``GET .../secrets``). Its ``items`` carry no ``kind`` of their own, so
#: the walk treats every kind-less item of such an envelope as a Secret.
SECRET_LIST_KIND = "SecretList"

#: The annotation ``kubectl apply`` writes with the full last manifest. On
#: a Secret it repeats every ``data`` / ``stringData`` value, and on a
#: HelmChart its Helm values. The walk redacts every annotation value on a
#: Secret and on a HelmChart (this one included); the CR row projection
#: also leaves this one out on every kind.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"

#: The two maps on a ``Secret`` whose values are secret material:
#: ``data`` (base64-encoded) and ``stringData`` (write-only plaintext,
#: which the API server normally folds into ``data`` but which a read of
#: an in-flight apply could still surface).
SECRET_DATA_FIELDS: tuple[str, ...] = ("data", "stringData")

#: The ``kind`` of the k3s / RKE2 ``helm.cattle.io/v1`` chart object and of
#: its list envelope. Its items carry no ``kind`` of their own either.
HELM_CHART_KIND = "HelmChart"
HELM_CHART_LIST_KIND = "HelmChartList"

#: The two ``spec`` fields of a ``HelmChart`` that hold Helm values:
#: ``valuesContent`` (one YAML text) and ``set`` (a map of single values).
HELM_CHART_VALUE_FIELDS: tuple[str, ...] = ("valuesContent", "set")

#: The ``kind`` of a kind-less item, by the ``kind`` of its list envelope.
_LIST_ITEM_KINDS: dict[str, str] = {
    SECRET_LIST_KIND: SECRET_KIND,
    HELM_CHART_LIST_KIND: HELM_CHART_KIND,
}

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
    Kubernetes ``Secret`` when its ``kind`` equals :data:`SECRET_KIND`, or
    when it has no ``kind`` and sits in the ``items`` of a
    :data:`SECRET_LIST_KIND` envelope (the shape ``GET .../secrets``
    returns). Each value of its ``data`` / ``stringData`` maps is replaced
    via :func:`redact_secret_value` (key names kept), and so is every
    value of its ``metadata.annotations`` (annotation keys kept). Deploy
    tools can copy the whole manifest into an annotation (for example
    :data:`LAST_APPLIED_ANNOTATION`), so no annotation value on a Secret
    is shown.
    A mapping whose ``kind`` is :data:`HELM_CHART_KIND` (or a kind-less
    item of a :data:`HELM_CHART_LIST_KIND` envelope) gets its
    ``spec.valuesContent`` and every ``spec.set`` value replaced the same
    way (:data:`HELM_CHART_VALUE_FIELDS`); the ``set`` keys stay. Its
    ``metadata.annotations`` values are replaced like a Secret's, because
    the last-applied annotation repeats the whole chart, values included.
    Every other value is walked so a ``Secret`` nested inside a
    ``List``-kind envelope or a ``k8s.cr.list`` ``items`` array is still
    caught. Scalars pass through unchanged. The input is never mutated --
    a new structure is returned, so a half-redacted object can never be
    left aliased elsewhere.
    """
    return _walk(value, implied_kind=None)


def _walk(value: Any, *, implied_kind: str | None) -> Any:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind is None:
            kind = implied_kind
        is_secret = kind == SECRET_KIND
        is_chart = kind == HELM_CHART_KIND
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret and key in SECRET_DATA_FIELDS and isinstance(item, dict):
                out[key] = {
                    inner_key: redact_secret_value(inner_val)
                    for inner_key, inner_val in item.items()
                }
            elif (is_secret or is_chart) and key == "metadata" and isinstance(item, dict):
                out[key] = _redact_metadata(item)
            elif is_chart and key == "spec" and isinstance(item, dict):
                out[key] = _redact_chart_spec(item)
            elif key == "items" and isinstance(item, list):
                item_kind = _LIST_ITEM_KINDS.get(kind) if isinstance(kind, str) else None
                out[key] = [_walk(entry, implied_kind=item_kind) for entry in item]
            else:
                out[key] = _walk(item, implied_kind=None)
        return out
    if isinstance(value, list):
        return [_walk(item, implied_kind=None) for item in value]
    return value


def _redact_chart_spec(spec: dict[Any, Any]) -> dict[Any, Any]:
    """Copy a ``HelmChart`` ``spec`` with every Helm value redacted.

    ``valuesContent`` becomes one placeholder. ``set`` keeps its keys and
    gets a placeholder per value. The other fields are walked as usual.
    """
    out: dict[Any, Any] = {}
    for key, item in spec.items():
        if key not in HELM_CHART_VALUE_FIELDS or item is None:
            out[key] = _walk(item, implied_kind=None)
        elif isinstance(item, dict):
            out[key] = {inner_key: redact_secret_value(val) for inner_key, val in item.items()}
        else:
            out[key] = redact_secret_value(item)
    return out


def _redact_metadata(metadata: dict[Any, Any]) -> dict[Any, Any]:
    """Copy a Secret's or HelmChart's ``metadata`` with every annotation value redacted.

    Annotation keys stay, so a read still shows which annotations exist.
    """
    out: dict[Any, Any] = _walk(metadata, implied_kind=None)
    annotations = out.get("annotations")
    if isinstance(annotations, dict):
        out["annotations"] = {key: redact_secret_value(value) for key, value in annotations.items()}
    return out
