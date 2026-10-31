# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the Cloud Builder operations share: job states, steps, errors and secrets.

Cloud Builder runs a validation or a bring-up by itself after the start call
returns. The operations in this package follow the vendor job rules:

* a start returns ``accepted`` with the vendor's id and the status read that
  follows it, never "done";
* a status read gives the vendor's state, MEHO's state word, the progress and
  the vendor's error text, or ``not_reachable`` with the id and the time when
  the appliance does not answer;
* a change that runs more than one step returns each step: ``done``,
  ``failed``, ``unknown`` (sent, but the reply was lost) or ``not_run``.

This module also holds :func:`fill_secrets`, which reads the passwords a call
names by Vault reference and puts them into a copy of the spec. The values
never go into a result: :func:`hide_secrets` replaces a vendor text that holds
one, and :class:`SecretMemory` keeps the values a start read and the ids of
the jobs it started, so a status read made later (which has no spec) can hide
them too. For a job this MEHO did not start (for example one started before
a restart), :func:`hide_vendor_texts` hides every vendor error text, because
MEHO cannot know which values it may echo. A text shaped like a VMware
licence key is hidden by its shape in every case.
"""

from __future__ import annotations

import copy
import re
import ssl
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "ACTIVE_VALIDATION_VENDOR_STATES",
    "DEFAULT_SECRET_KEY",
    "SDDC_STATES",
    "VALIDATION_STATES",
    "SecretInputError",
    "SecretMemory",
    "StepLog",
    "error_list_text",
    "fill_secrets",
    "hide_secrets",
    "hide_secrets_everywhere",
    "hide_vendor_texts",
    "not_reachable",
    "now_iso",
    "sddc_state",
    "unreachable_reason",
    "validation_state",
    "vendor_error_text",
]

#: The key in a Vault secret that holds the value, unless the call names another.
DEFAULT_SECRET_KEY: Final = "password"

#: The text put in place of a vendor text that holds a secret value.
_HIDDEN_TEXT: Final = "[MEHO hid this text: it held a secret from Vault]"

#: A VMware licence key: five groups of five letters or digits. A vendor text
#: that names a rejected key is hidden by this shape, so MEHO needs no memory
#: of the value.
_LICENCE_KEY_SHAPE: Final = re.compile(r"\b[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}\b")
_HIDDEN_KEY_TEXT: Final = "[MEHO hid a licence key]"

#: The text put in place of a vendor error text of a job this MEHO did not
#: start. MEHO cannot know which secret values such a text may echo.
_UNKNOWN_JOB_TEXT: Final = (
    "[MEHO hid this text: it may hold a secret of the spec, and MEHO did not start "
    "this job (for example it started before a restart of MEHO). Read the report, "
    "or start the job again]"
)

#: How many secret values and job ids :class:`SecretMemory` keeps per target
#: (the newest).
_SECRET_MEMORY_SIZE: Final = 64

#: ``executionStatus`` values of a validation that is still active on Cloud
#: Builder. ``CANCELLATION_IN_PROGRESS`` counts: the validation still runs
#: until the cancel is done, so a caller must keep polling and no second
#: validation may start.
ACTIVE_VALIDATION_VENDOR_STATES: Final[frozenset[str]] = frozenset(
    {"IN_PROGRESS", "CANCELLATION_IN_PROGRESS"}
)

#: ``SddcTask.status`` (bring-up) and MEHO's state word.
_SDDC_STATE_WORDS: Final[dict[str, str]] = {
    "IN_PROGRESS": "running",
    "COMPLETED_WITH_SUCCESS": "succeeded",
    "COMPLETED_WITH_FAILURE": "failed",
    "ROLLBACK_SUCCESS": "rolled_back",
}
#: Every ``state`` a bring-up read returns.
SDDC_STATES: Final[tuple[str, ...]] = (*_SDDC_STATE_WORDS.values(), "unknown", "not_reachable")

#: Every ``state`` a validation read returns.
VALIDATION_STATES: Final[tuple[str, ...]] = (
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
    "not_reachable",
)


class SecretInputError(Exception):
    """A secret reference of the call cannot be used. Nothing was sent.

    The message names the reference and the spec path, never a value.
    """


def now_iso() -> str:
    """The time now, ISO 8601 in UTC (when a read was made)."""
    return datetime.now(UTC).isoformat()


def sddc_state(vendor_state: object) -> str:
    """MEHO's state word for a bring-up ``SddcTask.status``."""
    if isinstance(vendor_state, str):
        return _SDDC_STATE_WORDS.get(vendor_state, "unknown")
    return "unknown"


def validation_state(execution_status: object, result_status: object) -> str:
    """MEHO's state word for a validation (``executionStatus`` + ``resultStatus``)."""
    if execution_status in ACTIVE_VALIDATION_VENDOR_STATES:
        return "running"
    if execution_status == "CANCELLED":
        return "cancelled"
    if execution_status == "FAILED" or result_status == "FAILED":
        return "failed"
    if execution_status == "COMPLETED" and result_status in ("SUCCEEDED", "FAILED_WITH_WARNINGS"):
        return "succeeded"
    return "unknown"


def unreachable_reason(exc: httpx.TransportError) -> str:
    """Why Cloud Builder did not answer: ``timeout``, ``tls_verify_failed``, ``connect_failed``."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    cause: BaseException | None = exc.__cause__ or exc.__context__
    if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return "tls_verify_failed"
    return "connect_failed"


def not_reachable(
    exc: httpx.TransportError, *, id_key: str, job_id: str | None, target: Any
) -> dict[str, Any]:
    """The answer of a status read when the appliance does not answer.

    It never says ``failed``: the job may still run on the appliance.
    """
    return {
        "state": "not_reachable",
        id_key: job_id,
        "target": str(getattr(target, "name", "")),
        "reason": unreachable_reason(exc),
        "checked_at": now_iso(),
        "guidance": (
            "Cloud Builder did not answer. This does not mean the job failed. "
            "Keep the id and read the status again later."
        ),
    }


def hide_secrets(text: str | None, secrets: Iterable[str]) -> str | None:
    """*text*, or a fixed note when it holds one of *secrets*.

    A licence key in *text* is replaced by a note, whether or not its value
    is in *secrets*.
    """
    if text is None:
        return None
    if any(secret and secret in text for secret in secrets):
        return _HIDDEN_TEXT
    return _LICENCE_KEY_SHAPE.sub(_HIDDEN_KEY_TEXT, text)


def hide_secrets_everywhere(value: Any, secrets: Iterable[str]) -> Any:
    """A copy of *value* with :func:`hide_secrets` applied to every text, at any depth."""
    found = tuple(secret for secret in secrets if secret)
    if isinstance(value, str):
        return hide_secrets(value, found)
    if isinstance(value, dict):
        return {key: hide_secrets_everywhere(item, found) for key, item in value.items()}
    if isinstance(value, list):
        return [hide_secrets_everywhere(item, found) for item in value]
    return value


def hide_vendor_texts(value: Any, keys: Iterable[str]) -> Any:
    """A copy of *value* where every text under one of *keys*, at any depth, is a note.

    For a job this MEHO did not start: the vendor texts may echo secret
    values MEHO has no memory of, so none of them is shown. Empty texts and
    ``None`` stay as they are, so "no error" is still visible.
    """
    hidden = frozenset(keys)
    if isinstance(value, dict):
        return {
            key: _UNKNOWN_JOB_TEXT
            if key in hidden and isinstance(item, str) and item
            else hide_vendor_texts(item, hidden)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [hide_vendor_texts(item, hidden) for item in value]
    return value


class SecretMemory:
    """What the starts sent to each Cloud Builder, kept in memory.

    A status read runs in a later call and has no spec, so it cannot read the
    secrets from Vault again. The connector remembers, by the host of the
    target, the values a start read and the ids of the jobs it started. A
    later read of a job it started hides the values in the vendor texts and
    in the flight-recorder body. A read of a job it did not start (one from
    before a restart of MEHO, or started by someone else) shows no vendor
    error text at all: MEHO cannot know which values such a text may echo.
    Only the newest values and ids are kept.
    """

    def __init__(self) -> None:
        self._values: dict[str, deque[str]] = {}
        self._jobs: dict[str, deque[str]] = {}

    @staticmethod
    def key(host: object) -> str:
        return str(host or "").strip().lower()

    def remember(self, host: object, values: Iterable[str]) -> None:
        known = self._values.setdefault(self.key(host), deque(maxlen=_SECRET_MEMORY_SIZE))
        for value in values:
            if value and value not in known:
                known.append(value)

    def known(self, host: object) -> tuple[str, ...]:
        return tuple(self._values.get(self.key(host), ()))

    def remember_job(self, host: object, job_id: object) -> None:
        """Note that this MEHO started the job *job_id* on *host*."""
        if not job_id:
            return
        jobs = self._jobs.setdefault(self.key(host), deque(maxlen=_SECRET_MEMORY_SIZE))
        if str(job_id) not in jobs:
            jobs.append(str(job_id))

    def knows_job(self, host: object, job_id: object) -> bool:
        """``True`` when this MEHO started the job *job_id* on *host*."""
        return bool(job_id) and str(job_id) in self._jobs.get(self.key(host), ())

    def clear(self) -> None:
        self._values.clear()
        self._jobs.clear()


def _error_texts(error: Mapping[str, Any]) -> list[str]:
    """``message`` and ``remediationMessage`` of a vendor ``Error`` and its nested errors."""
    texts = [
        str(error[key])
        for key in ("message", "remediationMessage")
        if isinstance(error.get(key), str) and error[key]
    ]
    for nested in error.get("nestedErrors") or ():
        if isinstance(nested, Mapping):
            texts.extend(_error_texts(nested))
    return texts


def error_list_text(errors: object, secrets: Iterable[str] = ()) -> str | None:
    """The joined texts of a list of vendor ``Error`` objects, or ``None``."""
    if not isinstance(errors, list):
        return None
    texts: list[str] = []
    for error in errors:
        if isinstance(error, Mapping):
            texts.extend(_error_texts(error))
    if not texts:
        return None
    return hide_secrets("; ".join(texts), tuple(secrets))


def vendor_error_text(exc: httpx.HTTPStatusError, secrets: Iterable[str] = ()) -> str:
    """``HTTP <status>: <vendor message>`` from the response only (never the request)."""
    status = exc.response.status_code
    detail: str | None = None
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        detail = error_list_text([body], secrets)
    elif not body and exc.response.text:
        detail = hide_secrets(exc.response.text[:500], tuple(secrets))
    return f"HTTP {status}" + (f": {detail}" if detail else "")


class StepLog:
    """The steps of a multi-step change and how each one went."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._rows: list[dict[str, Any]] = [
            {"step": name, "result": "not_run", "error": None} for name in names
        ]

    def done(self, name: str) -> None:
        self._set(name, "done", None)

    def failed(self, name: str, error: str) -> None:
        self._set(name, "failed", error)

    def unknown(self, name: str, error: str) -> None:
        """The step was sent, but its reply was lost."""
        self._set(name, "unknown", error)

    def _set(self, name: str, result: str, error: str | None) -> None:
        for row in self._rows:
            if row["step"] == name:
                row["result"], row["error"] = result, error

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


@dataclass(slots=True)
class _SecretRefTarget:
    """Points the shared Vault read at a path the call names (not the target's login)."""

    name: str
    host: str
    secret_ref: str | None


def _pointer_tokens(pointer: str) -> list[str]:
    """The tokens of a JSON Pointer (RFC 6901), for example ``/a/0/b``."""
    if not pointer.startswith("/"):
        raise SecretInputError(f"spec path {pointer!r} must start with '/'")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def _set_at_pointer(document: Any, pointer: str, value: str) -> None:
    """Put *value* at *pointer* in *document*. The parent must exist."""
    tokens = _pointer_tokens(pointer)
    node = document
    for token in tokens[:-1]:
        node = _child(node, token, pointer)
    last = tokens[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and last.isdigit() and int(last) < len(node):
        node[int(last)] = value
    else:
        raise SecretInputError(f"spec path {pointer!r} does not point into the spec")


def _child(node: Any, token: str, pointer: str) -> Any:
    if isinstance(node, dict) and token in node:
        return node[token]
    if isinstance(node, list) and token.isdigit() and int(token) < len(node):
        return node[int(token)]
    raise SecretInputError(f"spec path {pointer!r} does not point into the spec")


async def fill_secrets(
    operator: Operator, target: Any, spec: Mapping[str, Any], refs: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Read each secret reference from Vault and put its value into a copy of *spec*.

    Each reference is ``{path, secret_ref, secret_key?, secret_mount?}``:
    ``path`` is a JSON Pointer into the spec. The read runs under the caller's
    identity. Returns the filled copy and the values (to hide them from vendor
    texts). A Vault refusal raises
    :class:`~meho_backplane.connectors._shared.vault_creds.SecretRefReadError`;
    a secret with no text under its key, or a path outside the spec, raises
    :class:`SecretInputError`.
    """
    filled = copy.deepcopy(dict(spec))
    values: list[str] = []
    for ref in refs:
        pointer = str(ref.get("path") or "")
        secret_ref = str(ref.get("secret_ref") or "").strip()
        key = str(ref.get("secret_key") or DEFAULT_SECRET_KEY).strip()
        mount = str(ref.get("secret_mount") or DEFAULT_KV_MOUNT).strip()
        data = await load_param_secret_data(
            _SecretRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
            operator,
            param="secrets",
            mount=mount,
        )
        raw = data.get(key) if isinstance(data, dict) else None
        value = strip_credential_value(raw) if isinstance(raw, str) else ""
        if not value:
            raise SecretInputError(
                f"the Vault secret {secret_ref!r} (mount {mount!r}) has no text under the "
                f"key {key!r} (for spec path {pointer!r})"
            )
        _set_at_pointer(filled, pointer, value)
        values.append(value)
    return filled, tuple(values)
