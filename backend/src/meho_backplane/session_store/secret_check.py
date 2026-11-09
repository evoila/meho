# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret check that every uploaded file passes before MEHO stores it.

Two steps for each file part:

1. **Known secrets are replaced.** The named patterns of the redaction
   engine (:mod:`meho_backplane.redaction.patterns`) that find secrets
   with a clear shape -- a bearer token, a JWT, a private key, a
   ``password=...`` pair and so on -- are replaced with
   ``[REDACTED:<pattern>]``.
2. **Unclear content goes to quarantine.** The check is not sure when:

   * the bytes are not UTF-8 text, so the patterns cannot read them; or
   * a secret-like name still has a value after step 1, for example
     ``"password": "..."`` in JSON or in escaped JSON, which the
     ``api_key`` pattern does not cover because of the quote after the
     name.

   Such a part is still stored, with step 1 applied, but its upload goes
   to the quarantine prefix of the bucket. Only an admin can open it.

The check is pure: no I/O and no logging. The result never holds a found
value, only counts and pattern names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Final

from meho_backplane.redaction.patterns import get_pattern, secret_spans

__all__ = ["CheckedPart", "check_part"]

#: Patterns with a clear secret shape. ``uuid``, ``ipv4``, ``ipv6`` and
#: ``fqdn`` are left out: they are not secrets, and a transcript needs
#: them to make sense.
KNOWN_SECRET_PATTERNS: Final[tuple[str, ...]] = (
    "pem_private_key",
    "kubeconfig",
    "authorization_header",
    "bearer_token",
    "jwt",
    "url_secret",
    "password_hash",
    "ceph_key",
    "bind_key_secret",
    "api_key",
)

# A secret-like name, maybe quoted or escaped, then ``:`` or ``=``, then a
# value of at least 6 characters. The value stops at whitespace, a quote, a
# backslash, a comma or a closing brace.
_UNCLEAR_SECRET: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])(?:password|passwd|pwd|passphrase|secret|client[_-]?secret"
    r"|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|token)"
    r"(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?"
    r"(?P<value>[^\s\"'\\,}]{6,})",
    re.IGNORECASE,
)

#: Values that are not a secret: a marker, a variable or a placeholder.
_NOT_A_VALUE: Final[re.Pattern[str]] = re.compile(r"^(?:\[REDACTED|\$|<|\{|\*{3})")


@dataclass(frozen=True)
class CheckedPart:
    """The checked bytes of one file part and what the check found."""

    content: bytes
    redactions: dict[str, int] = field(default_factory=dict)
    unclear_reasons: tuple[str, ...] = ()

    @property
    def unclear(self) -> bool:
        """Whether this part must go to quarantine."""
        return bool(self.unclear_reasons)


def _replace_known(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name in KNOWN_SECRET_PATTERNS:
        spans = secret_spans(get_pattern(name), text)
        if not spans:
            continue
        counts[name] = len(spans)
        marker = f"[REDACTED:{name}]"
        pieces: list[str] = []
        last = 0
        for start, end in spans:
            pieces.append(text[last:start])
            pieces.append(marker)
            last = end
        pieces.append(text[last:])
        text = "".join(pieces)
    return text, counts


def _has_unclear_secret(text: str) -> bool:
    return any(
        not _NOT_A_VALUE.match(match.group("value")) for match in _UNCLEAR_SECRET.finditer(text)
    )


#: One line with its ``\n``, or the last line without one.
_LINE: Final[re.Pattern[str]] = re.compile(r"[^\n]*\n|[^\n]+")

_SECRET_NAME: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_-])(?:password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key"
    r"|access[_-]?key|secret[_-]?key|private[_-]?key|token)$",
    re.IGNORECASE,
)


class _JsonWalk:
    """Check every string of one parsed JSON value, and every name with its value."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.unclear = False

    def text(self, value: str) -> str:
        cleaned, counts = _replace_known(value)
        for name, count in counts.items():
            self.counts[name] = self.counts.get(name, 0) + count
        if _has_unclear_secret(cleaned):
            self.unclear = True
        return cleaned

    def walk(self, node: object) -> object:
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self.walk(item) for item in node]
        if isinstance(node, dict):
            result: dict[str, object] = {}
            for key, value in node.items():
                checked = self.walk(value)
                if (
                    _SECRET_NAME.search(key)
                    and isinstance(checked, str)
                    and len(checked) >= 6
                    and not _NOT_A_VALUE.match(checked)
                ):
                    self.unclear = True
                result[key] = checked
            return result
        return node


def _check_line(line: str, counts: dict[str, int]) -> tuple[str, bool]:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    try:
        parsed = json.loads(body) if body.lstrip().startswith(("{", "[")) else None
    except ValueError:
        parsed = None
    if parsed is None:
        cleaned, found = _replace_known(body)
        unclear = _has_unclear_secret(cleaned)
    else:
        walker = _JsonWalk()
        checked = walker.walk(parsed)
        found, unclear = walker.counts, walker.unclear
        cleaned = (
            body if not found else json.dumps(checked, ensure_ascii=False, separators=(",", ":"))
        )
    for name, count in found.items():
        counts[name] = counts.get(name, 0) + count
    return cleaned + ending, unclear


def check_part(raw: bytes) -> CheckedPart:
    """Check one file part line by line: replace known secrets, say when it is unclear.

    A line that is a JSON object or array (a JSON Lines transcript) is
    parsed, and each string in it is checked. So a replacement never breaks
    the JSON, and a ``"password": "..."`` pair is seen as a name and a
    value. A line that changed is written back as compact JSON; a line with
    no finding keeps its bytes. Any other line is checked as plain text.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return CheckedPart(content=raw, unclear_reasons=("not_utf8_text",))
    counts: dict[str, int] = {}
    unclear = False
    lines: list[str] = []
    for line in _LINE.findall(text):
        cleaned, line_unclear = _check_line(line, counts)
        lines.append(cleaned)
        unclear = unclear or line_unclear
    reasons = ("possible_secret",) if unclear else ()
    return CheckedPart(
        content="".join(lines).encode("utf-8"), redactions=counts, unclear_reasons=reasons
    )
