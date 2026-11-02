# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret values in a dispatch answer: what the caller sees, what the audit row stores.

Two jobs, both run by the dispatcher right after the connector-boundary
redaction:

* :func:`hide_secret_values` builds the default view of an answer. For an
  operation in :data:`_VALUE_HIDING_OPS` (a Vault secret read, the guest
  environment read) every value is hidden and only the names stay. For any
  other operation, values under secret-shaped key names are hidden, numbers
  included, except the fields the operation declared as plain
  (:func:`register_plain_fields`). The caller of every operation gets this
  view unless a person or a service principal passes ``reveal_secret: true``.
  An operation that makes a new credential is the exception: the dispatcher
  returns it to the caller.
* :func:`reveal_hidden_values` builds the answer of a reveal: the redacted
  answer, with every value the default view hides put back as the
  connector returned it. The redaction of the other fields stays.
  ``reveal_secret`` is a dispatch control, not a parameter of the
  operation: :func:`split_reveal_secret` takes it off the params before
  they are hashed, checked and handed to the connector, and
  :func:`with_reveal_secret` puts it back on the params an approval
  request stores, so an approved run keeps the choice.
* :func:`fingerprint_hidden_values` builds the copy of the raw answer that
  goes to ``audit_log.raw_payload``. Every value that differs between the
  raw answer and the default view is replaced by a keyed fingerprint
  (HMAC-SHA256 under the server key, the same key as the audit params
  hash). Equal values give equal fingerprints inside one install, so an
  auditor can still tell that two rows held the same value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from meho_backplane.broadcast.events import scrub_secret_named_values
from meho_backplane.operations._validate import compute_audit_digest_hash

__all__ = [
    "FINGERPRINT_PREFIX",
    "REVEAL_SECRET_PARAM",
    "SECRET_VALUE_PLACEHOLDER",
    "fingerprint_hidden_values",
    "fingerprint_value",
    "fingerprinted_paths",
    "hide_secret_values",
    "hides_secret_values",
    "plain_fields_of",
    "register_plain_fields",
    "reveal_hidden_values",
    "split_reveal_secret",
    "with_reveal_secret",
]

#: What a hidden value of a secret read is replaced with.
SECRET_VALUE_PLACEHOLDER: Final[str] = "[REDACTED:secret_value]"

#: Start of a fingerprint in ``audit_log.raw_payload``.
FINGERPRINT_PREFIX: Final[str] = "[FINGERPRINT:hmac-sha256:"

#: The dispatch control a person or a service principal sets to get the
#: hidden values of an answer back.
REVEAL_SECRET_PARAM: Final[str] = "reveal_secret"

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


#: Per operation, the fields whose name looks like a secret but whose value
#: is not one. Filled at import time by the connector modules and by a
#: connector package's registrar, like the preview builders.
_PLAIN_FIELDS: Final[dict[str, frozenset[str]]] = {}


def split_reveal_secret(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return *params* without ``reveal_secret``, and whether it was set.

    The flag is not a parameter of the operation: it stays off the params
    hash, off the schema check and off the connector call. Without the
    flag *params* is returned as is.
    """
    if REVEAL_SECRET_PARAM not in params:
        return params, False
    reveal_secret = bool(params[REVEAL_SECRET_PARAM])
    without = {key: value for key, value in params.items() if key != REVEAL_SECRET_PARAM}
    return without, reveal_secret


def with_reveal_secret(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *params* with ``reveal_secret: true`` set."""
    return {**params, REVEAL_SECRET_PARAM: True}


def register_plain_fields(op_id: str, field_names: Iterable[str]) -> None:
    """Declare the fields of *op_id* that hold no secret although their name says so.

    The default view of every answer hides the value under a secret-shaped
    field name (``password``, ``token``, ``api_key``, ...). Some operations
    return such a name with a plain value: ``password_expires_at`` is a
    date, ``secret_id_ttl`` a number of seconds, ``image_pull_secrets`` a
    list of names, ``token`` in the RKE2 posture read the mode and owner of
    the token file. A connector, built-in or from a package, declares them
    here, and the view keeps each one with its whole value, at every depth
    of the answer. A field that is not declared stays hidden, so a
    forgotten declaration never shows a secret. Registering again replaces
    the earlier set.
    """
    _PLAIN_FIELDS[op_id] = frozenset(str(name) for name in field_names)


def plain_fields_of(op_id: str) -> frozenset[str]:
    """The declared plain fields of *op_id*; empty when none were declared."""
    return _PLAIN_FIELDS.get(op_id, frozenset())


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
    shown, _ = scrub_secret_named_values(
        answer, hide_numbers=True, plain_names=plain_fields_of(op_id)
    )
    return shown


def reveal_hidden_values(raw: Any, redacted: Any, shown: Any) -> Any:
    """Return *redacted* with every value that *shown* hides put back from *raw*.

    The answer of a ``reveal_secret: true`` by a person or a service
    principal. *shown* is *redacted*
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
