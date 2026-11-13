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
     name; or
   * a private key block in plain text starts in one upload and ends in
     another.

   Such a part is still stored, with step 1 applied, but its upload goes
   to the quarantine prefix of the bucket. Only an admin can open it.

A private key block in plain text spans many lines. Its ``BEGIN`` line
becomes one marker, and every line up to its ``END`` line is removed. A
block can start in an earlier upload: :func:`ends_inside_private_key`
reads the stored part of the file (an upload always sends the whole file)
and says whether the new part starts inside a key.

The check is pure: no I/O and no logging. The result never holds a found
value, only counts and pattern names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Final

from meho_backplane.redaction.patterns import MASK_CHARS, get_pattern, secret_spans

__all__ = ["CheckedPart", "check_part", "ends_inside_private_key"]

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

#: Values that are not a secret: a marker, a variable, a placeholder, or
#: only mask characters (``password=\u2022\u2022\u2022\u2022\u2022\u2022``).
_NOT_A_VALUE: Final[re.Pattern[str]] = re.compile(
    r"^(?:\[REDACTED|\$|<|\{|\*{3}|[" + MASK_CHARS + r"]+$)"
)


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


#: The ``BEGIN`` or ``END`` line of a private key block.
_KEY_MARK: Final[re.Pattern[str]] = re.compile(r"-----(BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_KEY_MARK_BYTES: Final[re.Pattern[bytes]] = re.compile(_KEY_MARK.pattern.encode())
_KEY_MARKER: Final[str] = "[REDACTED:pem_private_key]"


def _json_value(body: str) -> object | None:
    """Return the parsed JSON object or array of one line, or ``None``."""
    if not body.lstrip().startswith(("{", "[")):
        return None
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return None
    return parsed


def _opens_key(body: str) -> bool:
    """Whether a plain-text *body* ends inside a private key block."""
    marks = [match.group(1) for match in _KEY_MARK.finditer(body)]
    return bool(marks) and marks[-1] == "BEGIN"


def ends_inside_private_key(data: bytes, end: int) -> bool:
    """Whether ``data[:end]`` ends inside a private key block in plain text.

    Follows the same rules as :func:`check_part`: outside a key, a
    ``BEGIN`` line that is not a JSON line opens a key; inside a key, the
    next ``END`` closes it. Only lines with a mark are decoded.
    """
    inside = False
    for match in _KEY_MARK_BYTES.finditer(data, 0, end):
        if inside:
            inside = match.group(1) != b"END"
            continue
        if match.group(1) != b"BEGIN":
            continue
        start = data.rfind(b"\n", 0, match.start()) + 1
        stop = data.find(b"\n", match.start(), end)
        line = data[start : stop if stop != -1 else end].decode("utf-8", errors="replace")
        inside = _json_value(line) is None
    return inside


def _check_line(line: str, counts: dict[str, int]) -> tuple[str, bool]:
    body = line.rstrip("\r\n")
    ending = line[len(body) :]
    parsed = _json_value(body)
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


class _KeyBlock:
    """Remove the lines of a private key block in plain text, line by line."""

    def __init__(self, counts: dict[str, int], *, inside: bool) -> None:
        self.counts = counts
        self.inside = inside
        self.started_before = inside

    def line(self, line: str) -> tuple[str, bool]:
        """Return the checked *line* and whether it is unclear."""
        if self.inside:
            end = next((m for m in _KEY_MARK.finditer(line) if m.group(1) == "END"), None)
            if end is None:
                return "", False
            self.inside = False
            line = line[end.end() :]
            if not line.strip():
                return "", False
        cleaned, unclear = _check_line(line, self.counts)
        body = line.rstrip("\r\n")
        if _json_value(body) is None and _opens_key(body):
            self.inside = True
        return cleaned, unclear


def check_part(raw: bytes, *, inside_private_key: bool = False) -> CheckedPart:
    """Check one file part line by line: replace known secrets, say when it is unclear.

    A line that is a JSON object or array (a JSON Lines transcript) is
    parsed, and each string in it is checked. So a replacement never breaks
    the JSON, and a ``"password": "..."`` pair is seen as a name and a
    value. A line that changed is written back as compact JSON; a line with
    no finding keeps its bytes. Any other line is checked as plain text.

    *inside_private_key* says that the part starts inside a private key
    block of an earlier upload (:func:`ends_inside_private_key`). Then its
    lines up to the ``END`` line become one marker. A block that starts or
    ends outside this part sends the part to quarantine.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return CheckedPart(content=raw, unclear_reasons=("not_utf8_text",))
    counts: dict[str, int] = {}
    unclear = False
    lines: list[str] = []
    block = _KeyBlock(counts, inside=inside_private_key)
    if inside_private_key:
        lines.append(_KEY_MARKER + "\n")
        counts["pem_private_key"] = 1
    for line in _LINE.findall(text):
        cleaned, line_unclear = block.line(line)
        lines.append(cleaned)
        unclear = unclear or line_unclear
    reasons: tuple[str, ...] = ()
    if unclear:
        reasons += ("possible_secret",)
    if block.started_before or block.inside:
        reasons += ("split_private_key",)
    return CheckedPart(
        content="".join(lines).encode("utf-8"), redactions=counts, unclear_reasons=reasons
    )
