# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret values in a dispatch answer: what the caller sees, what the audit row stores.

Two jobs, both run by the dispatcher right after the connector-boundary
redaction:

* :func:`hide_secret_values` builds the default view of an answer. For an
  operation in :data:`_VALUE_HIDING_OPS` (a Vault secret read, the guest
  environment read) every value is hidden and only the names stay. For any
  other operation, values under secret-shaped key names are hidden, numbers
  included. The caller of a secret read gets this view unless a person
  passes ``reveal_secret: true``.
* :func:`reveal_hidden_values` builds the answer of a person's reveal: the
  redacted answer, with every value the default view hides put back as the
  connector returned it. The redaction of the other fields stays.
* :func:`fingerprint_hidden_values` builds the copy of the raw answer that
  goes to ``audit_log.raw_payload``. Every value that differs between the
  raw answer and the default view is replaced by a keyed fingerprint
  (HMAC-SHA256 under the server key, the same key as the audit params
  hash). Equal values give equal fingerprints inside one install, so an
  auditor can still tell that two rows held the same value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.operations._validate import compute_audit_digest_hash

__all__ = [
    "FINGERPRINT_PREFIX",
    "SECRET_VALUE_PLACEHOLDER",
    "fingerprint_hidden_values",
    "fingerprint_value",
    "fingerprinted_paths",
    "hide_secret_values",
    "hides_secret_values",
    "reveal_hidden_values",
]

#: What a hidden value of a secret read is replaced with.
SECRET_VALUE_PLACEHOLDER: Final[str] = "[REDACTED:secret_value]"

#: Start of a fingerprint in ``audit_log.raw_payload``.
FINGERPRINT_PREFIX: Final[str] = "[FINGERPRINT:hmac-sha256:"

_MISSING: Final[object] = object()


def _hide_all(node: Any) -> Any:
    """Replace every leaf of *node* with the placeholder; keep keys and list sizes."""
    if isinstance(node, Mapping):
        return {str(key): _hide_all(value) for key, value in node.items()}
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        return [_hide_all(item) for item in node]
    return SECRET_VALUE_PLACEHOLDER


def _hide_kv_data(answer: Any, raw: Any) -> Any:
    """``vault.kv.read``: keep the key names under ``data``, hide every value.

    The other fields (``version``) carry no secret and stay. An answer of
    another shape is hidden whole.
    """
    del raw
    if not isinstance(answer, Mapping) or not isinstance(answer.get("data"), Mapping):
        return _hide_all(answer)
    shown, _ = scrub_secret_named_values(
        {key: value for key, value in answer.items() if key != "data"}, hide_numbers=True
    )
    shown["data"] = {str(key): SECRET_VALUE_PLACEHOLDER for key in answer["data"]}
    return shown


def _hide_env_value(entry: Any) -> Any:
    """``NAME=value`` becomes ``NAME=<placeholder>``; anything else is hidden whole."""
    if isinstance(entry, str) and "=" in entry:
        name, _, _value = entry.partition("=")
        return f"{name}={SECRET_VALUE_PLACEHOLDER}"
    return SECRET_VALUE_PLACEHOLDER


def _env_entries(answer: Mapping[str, Any], raw: Any) -> list[Any]:
    """The ``variables`` entries whose names are kept: the raw ones when they line up.

    A free-text rule may have replaced a whole ``DB_PASSWORD=...`` entry in
    the redacted answer, name included. The raw answer still has the name.
    """
    redacted_entries: list[Any] = answer["variables"]
    if isinstance(raw, Mapping) and isinstance(raw.get("variables"), list):
        raw_entries: list[Any] = raw["variables"]
        if len(raw_entries) == len(redacted_entries):
            return raw_entries
    return redacted_entries


def _hide_env_values(answer: Any, raw: Any) -> Any:
    """``vmware.composite.vm.guest.env.read``: keep the variable names, hide the values."""
    if not isinstance(answer, Mapping) or not isinstance(answer.get("variables"), list):
        return _hide_all(answer)
    shown, _ = scrub_secret_named_values(
        {key: value for key, value in answer.items() if key != "variables"}, hide_numbers=True
    )
    shown["variables"] = [_hide_env_value(entry) for entry in _env_entries(answer, raw)]
    return shown


#: Operations whose answer is made of secret values. By default the caller
#: gets the names only.
_VALUE_HIDING_OPS: Final[dict[str, Callable[[Any, Any], Any]]] = {
    "vault.kv.read": _hide_kv_data,
    "vmware.composite.vm.guest.env.read": _hide_env_values,
}


def hides_secret_values(op_id: str) -> bool:
    """True when the default answer of *op_id* holds names and no values."""
    return op_id in _VALUE_HIDING_OPS


def hide_secret_values(op_id: str, answer: Any, *, raw: Any = None) -> Any:
    """Return the default view of *answer* (see the module docstring).

    *answer* is the redacted answer. *raw* is the answer as the connector
    returned it; the guest environment read takes the variable names from
    it, because a free-text rule may have replaced a whole ``NAME=value``
    entry in *answer*.
    """
    hide = _VALUE_HIDING_OPS.get(op_id)
    if hide is not None:
        return hide(answer, raw)
    shown, _ = scrub_secret_named_values(answer, hide_numbers=True)
    return shown


def reveal_hidden_values(raw: Any, redacted: Any, shown: Any) -> Any:
    """Return *redacted* with every value that *shown* hides put back from *raw*.

    The answer of a person's ``reveal_secret: true``. *shown* is *redacted*
    after :func:`hide_secret_values`. Where the two differ, the default view
    hid a secret field, and the person gets the raw value of that field: a
    free-text rule in the redaction (a PEM block, a token) does not replace
    the value of a field the person asked to see. Where the two are equal,
    the redacted value stays, so the redaction of the other fields holds.
    """
    if isinstance(raw, Mapping) and isinstance(redacted, Mapping) and isinstance(shown, Mapping):
        return {
            key: reveal_hidden_values(raw.get(key, _MISSING), value, shown.get(key, _MISSING))
            for key, value in redacted.items()
        }
    if (
        isinstance(raw, list)
        and isinstance(redacted, list)
        and isinstance(shown, list)
        and len(raw) == len(redacted) == len(shown)
    ):
        return [
            reveal_hidden_values(raw_item, item, shown_item)
            for raw_item, item, shown_item in zip(raw, redacted, shown, strict=True)
        ]
    if raw is _MISSING or (type(redacted) is type(shown) and redacted == shown):
        return redacted
    return raw


def fingerprint_value(value: Any) -> str:
    """One keyed fingerprint of *value* (its canonical JSON)."""
    canonical = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return f"{FINGERPRINT_PREFIX}{compute_audit_digest_hash(canonical)}]"


def fingerprint_hidden_values(raw: Any, shown: Any) -> Any:
    """Return *raw* with every value that *shown* does not show as a fingerprint.

    *shown* is *raw* after redaction and :func:`hide_secret_values`. The two
    are walked side by side. A leaf that is equal in both stays as it is; a
    leaf or subtree that differs, or that *shown* does not have, becomes one
    fingerprint of its canonical JSON.
    """
    if isinstance(raw, Mapping) and isinstance(shown, Mapping):
        return {
            key: fingerprint_hidden_values(value, shown.get(key, _MISSING))
            for key, value in raw.items()
        }
    if isinstance(raw, list) and isinstance(shown, list) and len(raw) == len(shown):
        return [
            fingerprint_hidden_values(item, other) for item, other in zip(raw, shown, strict=True)
        ]
    if shown is not _MISSING and type(raw) is type(shown) and raw == shown:
        return raw
    return fingerprint_value(raw)


def fingerprinted_paths(node: Any, path: str = "") -> frozenset[str]:
    """Dotted paths of the fingerprints in a stored raw payload.

    Same path spelling as the redaction manifest (``data.password``,
    ``items.0.token``), so the policy replay can leave out manifest entries
    whose raw value is no longer stored.
    """
    if isinstance(node, str):
        return frozenset({path}) if node.startswith(FINGERPRINT_PREFIX) else frozenset()
    if isinstance(node, Mapping):
        children: Any = ((str(key), value) for key, value in node.items())
    elif isinstance(node, list):
        children = ((str(index), item) for index, item in enumerate(node))
    else:
        return frozenset()
    found: set[str] = set()
    for name, child in children:
        found |= fingerprinted_paths(child, f"{path}.{name}" if path else name)
    return frozenset(found)
