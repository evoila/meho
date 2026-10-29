# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hide the vendor's error text of an operation that sends a secret (#4854).

An operation that sends a secret (a licence key, a new password, a secret
value) can get an error whose vendor text repeats that secret: a fault
string such as "the license key <key> is not valid". The error builders in
:mod:`._errors` copy that text into the error result, and the dispatcher
copies the result's ``extras`` into the audit row and, for an approved run,
onto the approval request (``resume_result``).

So the dispatcher runs every error result through
:func:`withhold_vendor_text` before it writes the audit row (one place,
``dispatcher._audit_error_and_return``). For an operation that sends a
secret, each field that holds vendor text gets the fixed text
:data:`VENDOR_TEXT_WITHHELD`. The fields MEHO writes itself stay:
``error_code``, ``exception_class``, ``http_status``, ``cause``,
``remediation`` and the fault type ``upstream_fault.type``. The shape of the
result does not change.

The rule runs on the built result, not on the raised error. So the
dispatcher still sorts the error by its class and status as before: the new
login and one retry after HTTP 401, and the 403 and 429 results.

For every operation, :func:`redact_error_result` then removes secrets from
the whole error result (#5031). The error builders clean each free text, but
some fields come straight from the vendor (the ``errors[]`` list of an HTTP
422, the permission headers of a 403), and a connector can add its own. So
the same place cleans every string and every field of ``error``, ``extras``
and ``result``: the value of each secret-named field is replaced, then the
Tier-1 default policy runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors import OperationResult
from meho_backplane.operations._errors import UPSTREAM_SAID, VAULT_SAID
from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.flight_recorder.families import secret_tag_tokens
from meho_backplane.redaction.policy import RedactionPolicy
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.redaction.secret_fields import scrub_secret_fields

__all__ = [
    "ERROR_TEXT_WITHHELD",
    "VENDOR_TEXT_WITHHELD",
    "redact_error_result",
    "sends_secret",
    "withhold_vendor_text",
]

#: The text put in place of each vendor text of an operation that sends a secret.
VENDOR_TEXT_WITHHELD: Final = "<vendor text withheld: this operation sends a secret>"

#: The error summary when the redaction itself fails. The result then keeps
#: only its ``error_code``: an error result that may hold a secret is not sent.
ERROR_TEXT_WITHHELD: Final = "<error text withheld: redaction unavailable>"

#: ``extras`` keys whose value is text from the vendor or from a raised error.
_VENDOR_TEXT_KEYS: Final = ("upstream_message", "exception_message", "raw_message")

#: The ``error`` summary markers after which the vendor's text follows.
_SUMMARY_MARKERS: Final = (UPSTREAM_SAID, VAULT_SAID)


def sends_secret(op_id: str, tags: Iterable[str] | None) -> bool:
    """Whether the operation sends a secret, so its vendor error text must not be kept.

    Yes when :func:`~meho_backplane.broadcast.events.classify_op` pins the op
    as ``credential_write``, or when one of its tags has a secret token
    (:data:`~meho_backplane.redaction.flight_recorder.families.SECRET_FAMILY_TAGS`,
    for example the ``key`` of ``license-key``). The flight recorder uses the
    same tag list, so both agree.
    """
    return classify_op(op_id) == "credential_write" or bool(secret_tag_tokens(tags or ()))


def _summary_without_vendor_text(error: str | None) -> str | None:
    """Cut the summary at the first marker in the text, whichever marker it is.

    The vendor text itself can hold a later marker (a Vault message that
    says "Upstream said:"). Cutting at the first marker found in list order
    would keep the vendor text in front of that later marker.
    """
    if error is None:
        return None
    hits = [(error.find(marker), marker) for marker in _SUMMARY_MARKERS]
    found = [(index, marker) for index, marker in hits if index != -1]
    if not found:
        return error
    index, marker = min(found)
    return f"{error[:index]}{marker}{VENDOR_TEXT_WITHHELD}"


def _extras_without_vendor_text(extras: dict[str, Any]) -> dict[str, Any]:
    for key in _VENDOR_TEXT_KEYS:
        if isinstance(extras.get(key), str):
            extras[key] = VENDOR_TEXT_WITHHELD
    fault = extras.get("upstream_fault")
    if isinstance(fault, dict) and fault.get("faultstring") is not None:
        extras["upstream_fault"] = {**fault, "faultstring": VENDOR_TEXT_WITHHELD}
    if extras.get("error_code") == "connector_http_422" and extras.get("validation_errors"):
        extras["validation_errors"] = [VENDOR_TEXT_WITHHELD]
    return extras


def withhold_vendor_text(
    result: OperationResult, *, op_id: str, tags: Iterable[str] | None
) -> OperationResult:
    """Return *result* with its vendor text replaced, when the operation sends a secret.

    *result* is returned as it is for any other operation.
    """
    if not sends_secret(op_id, tags):
        return result
    return result.model_copy(
        update={
            "error": _summary_without_vendor_text(result.error),
            "extras": _extras_without_vendor_text(dict(result.extras)),
        }
    )


def _redacted(node: Any, policy: RedactionPolicy) -> Any:
    return redact(scrub_secret_fields(node), policy).redacted


def redact_error_result(result: OperationResult) -> OperationResult:
    """Return *result* with secrets removed from its ``error``, ``extras`` and ``result``.

    Runs on every error result, for every operation. ``result`` is set on an
    error a handler returned (#4274) and on a multi-step operation's error
    envelope (#3809); both can carry the vendor's detail. The default policy
    is pinned (:func:`~meho_backplane.redaction.resolver.get_default_policy`),
    the same as the error builders use, so a registered shadow-mode policy
    cannot turn it off. If the redaction fails, the result keeps only its
    ``error_code`` and the summary :data:`ERROR_TEXT_WITHHELD`: ``result`` and
    ``handle`` are dropped and ``delivery`` is ``"unavailable"``.
    """
    try:
        policy = get_default_policy()
        error = _redacted(result.error, policy)
        extras = _redacted(dict(result.extras), policy)
        payload = _redacted(result.result, policy)
    except Exception:  # fail-closed: never send an error result that may hold a secret
        kept = {key: result.extras[key] for key in ("error_code",) if key in result.extras}
        return result.model_copy(
            update={
                "error": ERROR_TEXT_WITHHELD,
                "extras": kept,
                "result": None,
                "handle": None,
                "delivery": "unavailable",
            }
        )
    return result.model_copy(update={"error": error, "extras": extras, "result": payload})
