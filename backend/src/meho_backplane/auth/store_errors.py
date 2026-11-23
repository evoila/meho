# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What MEHO shows and what it logs when the secret store fails.

An answer holds a sentence MEHO wrote (:func:`text_without_store_detail`).
The server log holds the store's own text, with the address and the secrets
replaced by a mark (:func:`store_text_for_log`, :func:`log_store_error`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

import hvac.exceptions
import structlog

from meho_backplane.redaction.engine import redact
from meho_backplane.redaction.resolver import get_default_policy
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from meho_backplane.redaction.secret_text import hide_secret_text
from meho_backplane.settings import get_settings

__all__ = [
    "STORE_ADDRESS_MARK",
    "STORE_FAILED_TEXT",
    "STORE_REFUSED_TEXT",
    "has_store_detail",
    "hide_store_address",
    "log_holds_store_text",
    "log_store_error",
    "log_without_store_text",
    "store_request_id",
    "store_status",
    "store_text_for_log",
    "text_without_store_detail",
]

_log = structlog.get_logger(__name__)

#: What replaces the secret store's address in any text MEHO shows or logs.
STORE_ADDRESS_MARK: Final = "<secret store>"

#: What an answer says when the store refuses a request.
STORE_REFUSED_TEXT: Final = "The secret store refused the request."

#: What an answer says for any other failure of the store.
STORE_FAILED_TEXT: Final = "The secret store returned an error."

_TEXT_WITHHELD: Final = "<store text withheld>"

#: A longer store text is withheld from the log, so the cleaning stays cheap.
#: Where a one-word host stands as an address: after ``//`` or ``@``, and in
#: the ``host='...'`` and ``resolve '...'`` forms of the HTTP client.
_ADDRESS_LEAD: Final = r"(?:(?<=//)|(?<=@)|(?<=host=')|(?<=host=\")|(?<=resolve ')|(?<=resolve \"))"

_LOG_INPUT_LIMIT: Final = 8192
_LOG_TEXT_CAP: Final = 2048

#: Set on a store error when its detail is in the log, so it is logged once.
_LOGGED_ATTR: Final = "_meho_store_error_logged"

#: How many links of an error chain are read.
_CHAIN_DEPTH: Final = 8

#: The status the store answered, by the class hvac raises for it.
_STATUS_BY_CLASS: Final = {
    type(hvac.exceptions.VaultError.from_status(code)).__name__: code
    for code in (400, 401, 403, 404, 429, 500, 501, 502, 503)
}

# The default policy finds a secret by a label and a value of 8 or more
# characters. The store also quotes values in sentences, writes short values
# and names its own tokens without a label. These rules cover those shapes.
# A quoted value has no sure end (an escape, a quote or a line break can stand
# in it), and neither has a labelled value with spaces. So the text is withheld
# from there on, and the words in front stay.
# A labelled value becomes "<label> <mark>": the log holds no label=value pair.
_STORE_TOKEN: Final = re.compile(
    r"\bhv[sbr]\.[A-Za-z0-9_-]{8,}|(?<![\w./-])[sbr]\.[A-Za-z0-9_-]{24,}"
)
_SECRET_WORD: Final = re.compile(
    r"passw|passphrase|passcode|pwd|token|secret(?!/| store>)|jwt|credential|psk|bearer"
    r"|(?:api|private|access|unseal)[ _-]?key",
    re.IGNORECASE,
)
#: A quote or an escape. An apostrophe inside a word (``doesn't``) is none.
_QUOTE_OR_ESCAPE: Final = re.compile(r"[\"`\\]|(?<!\w)'|'(?!\w)")
_PLAIN_VALUE: Final = re.compile(
    r"(?P<label>(?:password|passwd|passphrase|pwd|client_secret|secret_id"
    r"|token|secret|jwt|api_?key)\w{0,32}(?=\s*[=:]))"
    r"\s*(?P<sep>[=:])\s*(?P<rest>\S.*)",
    re.IGNORECASE | re.DOTALL,
)

#: The words after which the store writes the reason of a failed login.
_REASON_LABELS: Final = frozenset({"token", "secret", "jwt"})

#: Plain words of the store's own login error texts (Vault, OpenBao and the
#: JWT libraries they use). None holds a value or a colon. After a reason
#: label and a colon, the log keeps the rest only when each part of it,
#: split at ": ", is one of these.
_KNOWN_REASONS: Final = frozenset(
    phrase.casefold()
    for phrase in (
        "invalid role name",
        "token is expired",
        "permission denied",
        "signature is invalid",
        "error verifying token signature",
        "oidc",
        "malformed jwt",
        "go-jose/go-jose",
        "compact JWS format must have three parts",
        "invalid audience (aud) claim",
        "audience claim does not match any expected audience",
        "invalid issuer (iss) claim",
        "invalid subject (sub) claim",
        "invalid ID (jti) claim",
        "invalid not before (nbf) claim",
        "token not yet valid",
        "invalid expiration time (exp) claim",
        "invalid issued at (iat) claim",
        "token issued in the future",
        "no issued at (iat), not before (nbf), or expiration time (exp) claims in token",
        "token must be signed",
        "token signed with unexpected algorithm",
        "no known key successfully validated the token signature",
        "token missing kid header parameter",
        "error validating claims",
        "missing role",
        "missing token",
        "invalid token",
        "audience claim found in JWT but no audiences bound to the role",
        "role with oidc role_type is not allowed",
        "token is malformed",
        "token signature is invalid",
        "token is missing required claim",
        "token has invalid audience",
        "token used before issued",
        "go-jose/go-jose/jwt",
        "validation failed, token is expired (exp)",
        "validation failed, token not valid yet (nbf)",
        "validation failed, invalid issuer claim (iss)",
        "validation failed, invalid audience claim (aud)",
        "failed to verify id token signature",
        "id token not signed",
    )
)

#: Said in the log in place of a reason that holds words not on the list.
_REASON_WITHHELD: Final = (
    "(the store gave a reason that MEHO does not log; see the store's own log)"
)


_log_holds_text: ContextVar[bool] = ContextVar("store_error_log_holds_text", default=True)


@contextmanager
def log_without_store_text() -> Iterator[None]:
    """Log store request failures in this block without the store's text.

    The health check uses it: its log holds class names only. The event still
    has the error class, the store status and the request id.
    """
    token = _log_holds_text.set(False)
    try:
        yield
    finally:
        _log_holds_text.reset(token)


def log_holds_store_text() -> bool:
    """Say whether a store failure logged now may hold the store's text."""
    return _log_holds_text.get()


def hide_store_address(text: str) -> str:
    """Replace the configured secret store address in *text* with a fixed mark.

    The full address, the host with its port and the bare host are replaced.
    A one-word host (``vault``) is replaced only where it is used as an
    address (after ``//`` or ``@``, and in ``host='...'`` and
    ``resolve '...'``), so the same word in a sentence, a path, a quoted
    name or a field name stays. ``text``
    is returned unchanged when no address is configured.
    """
    try:
        addr = get_settings().vault_addr
    except Exception:  # never raise on an error path
        return text
    if addr is None:
        return text
    patterns = [re.escape(str(addr).rstrip("/"))]
    if addr.host:
        host = re.escape(addr.host)
        if addr.port:
            patterns.append(rf"{host}:{addr.port}(?!\d)")
        if "." in addr.host or ":" in addr.host:
            patterns.append(rf"(?<![\w-]){host}(?![\w-]|\.\d)")
        else:
            patterns.append(rf"{_ADDRESS_LEAD}{host}(?![\w-])")
    for pattern in patterns:
        text = re.sub(pattern, STORE_ADDRESS_MARK, text, flags=re.IGNORECASE)
    return text


def _withhold_from_first_quote(text: str) -> str:
    """Withhold *text* from its first quote or escape when it names a secret.

    The cut is at the start of the word that holds the quote, so no first part
    of a value stays.
    """
    quote = _QUOTE_OR_ESCAPE.search(text)
    if quote is None or not _SECRET_WORD.search(text):
        return text
    start = quote.start()
    while start and not text[start - 1].isspace():
        start -= 1
    return f"{text[:start]}{SECRET_FIELD_MARKER}"


def store_text_for_log(exc: BaseException) -> str:
    """Return the text of *exc* for the server log: no address and no secret.

    For an hvac error this is the store's own message, without the method and
    URL that hvac adds. Secrets are replaced first and the text is cut after.
    A part that cannot be cleaned safely is withheld, and so is the whole
    text when the cleaning fails.
    """
    try:
        if isinstance(exc, hvac.exceptions.VaultError):
            text = str(exc.args[0] or "") if exc.args else ""
        else:
            text = str(exc)
        if len(text) > _LOG_INPUT_LIMIT:
            return _TEXT_WITHHELD
        text = hide_store_address(text)
        reason = _known_reason(text)
        if reason is not None:
            text = text[: reason.end("label")]
        cleaned = _clean_for_log(text)
        if reason is not None:
            cleaned += _text_after_label(reason)
    except Exception:  # fail closed, and never raise on an error path
        return _TEXT_WITHHELD
    return cleaned[:_LOG_TEXT_CAP]


def _clean_for_log(text: str) -> str:
    text = hide_secret_text(_withhold_from_first_quote(text))
    text = _STORE_TOKEN.sub(SECRET_FIELD_MARKER, text)
    text = _PLAIN_VALUE.sub(_mark_labelled_value, text)
    return str(redact(text, get_default_policy()).redacted).strip()


def _is_reason_label(match: re.Match[str]) -> bool:
    """Say whether *match* is the plain word ``token``, ``secret`` or ``jwt`` and a colon."""
    start = match.start("label")
    before = match.string[start - 1] if start else ""
    return (
        match.group("label").casefold() in _REASON_LABELS
        and match.group("sep") == ":"
        and not (before.isalnum() or before == "_")
    )


def _known_reason(text: str) -> re.Match[str] | None:
    """Return the label match when the whole rest after it is listed reasons."""
    match = _PLAIN_VALUE.search(text)
    if match is None or not _is_reason_label(match):
        return None
    parts = match.group("rest").split(": ")
    if all(part.strip().casefold() in _KNOWN_REASONS for part in parts):
        return match
    return None


def _text_after_label(match: re.Match[str]) -> str:
    """Return the separator and the listed reasons after the label, as written."""
    return match.string[match.end("label") :].rstrip()


def _mark_labelled_value(match: re.Match[str]) -> str:
    marked = f"{match.group('label')} {SECRET_FIELD_MARKER}"
    return f"{marked} {_REASON_WITHHELD}" if _is_reason_label(match) else marked


def store_status(exc: BaseException) -> int | None:
    """Return the HTTP status the store answered, or ``None``."""
    return _STATUS_BY_CLASS.get(type(exc).__name__)


def store_request_id(exc: BaseException) -> str | None:
    """Return the store's request id from the error body, or ``None``."""
    body = getattr(exc, "json", None)
    value = body.get("request_id") if isinstance(body, dict) else None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
        return value
    return None


def _is_store_answer(exc: BaseException) -> bool:
    """Say whether *exc* is hvac's own error, whose text the store wrote.

    A MEHO subclass of an hvac error (``TargetSecretForbiddenError``) has a
    sentence MEHO wrote and is not a store answer.
    """
    return (
        isinstance(exc, hvac.exceptions.VaultError)
        and type(exc).__module__ == hvac.exceptions.__name__
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """Return *exc* and the errors behind it, up to a login error MEHO logged."""
    links: list[BaseException] = []
    link: BaseException | None = exc
    while link is not None and link not in links and len(links) < _CHAIN_DEPTH:
        links.append(link)
        if getattr(link, "store_text", None) is not None:
            break  # ``vault_login_failed`` has the detail of this one
        link = link.__cause__ or link.__context__
    return links


def has_store_detail(exc: BaseException) -> bool:
    """Say whether a traceback of *exc* would show the store's text or address."""
    for link in _chain(exc):
        if _is_store_answer(link):
            return True
        text = str(link)
        if hide_store_address(text) != text:
            return True
    return False


def log_store_error(exc: BaseException, *, op_id: str) -> None:
    """Log the store error behind *exc* once, as ``vault_request_failed``."""
    for link in _chain(exc):
        if _is_store_answer(link):
            if getattr(link, _LOGGED_ATTR, False):
                return
            setattr(link, _LOGGED_ATTR, True)
            _log.warning(
                "vault_request_failed",
                op_id=op_id,
                error_class=type(link).__name__,
                store_status=store_status(link),
                store_request_id=store_request_id(link),
                store_text=store_text_for_log(link) if log_holds_store_text() else None,
            )
            return


def text_without_store_detail(exc: BaseException) -> str:
    """Return the text of *exc* with MEHO's sentence in place of any store text.

    A store error becomes the sentence. So does the part of a wrapper's text
    that repeats the store error behind it. The store address is replaced in
    what is left. The caller still runs its own redaction.
    """
    if _is_store_answer(exc):
        return _store_sentence(exc)
    text = str(exc)
    for link in _chain(exc)[1:]:
        if _is_store_answer(link):
            for shown in (str(link), repr(link)):
                text = text.replace(shown, _store_sentence(link))
    return hide_store_address(text)


def _store_sentence(exc: BaseException) -> str:
    refused = isinstance(exc, hvac.exceptions.Forbidden)
    return STORE_REFUSED_TEXT if refused else STORE_FAILED_TEXT
