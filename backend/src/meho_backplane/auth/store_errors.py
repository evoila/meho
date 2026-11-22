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
    r"\s*[=:]\s*\S.*",
    re.IGNORECASE | re.DOTALL,
)
_LABELLED_MARK: Final = rf"\g<label> {SECRET_FIELD_MARKER}"


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
    address, so the same word in a sentence or a field name stays. ``text``
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
            patterns.append(rf"(?<=[/@'\"]){host}(?![\w-])")
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
        text = hide_secret_text(_withhold_from_first_quote(hide_store_address(text)))
        text = _STORE_TOKEN.sub(SECRET_FIELD_MARKER, text)
        text = _PLAIN_VALUE.sub(_LABELLED_MARK, text)
        cleaned = str(redact(text, get_default_policy()).redacted).strip()
    except Exception:  # fail closed, and never raise on an error path
        return _TEXT_WITHHELD
    return cleaned[:_LOG_TEXT_CAP]


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
