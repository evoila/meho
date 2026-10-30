# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""CI check: saved test captures hold no planted value and no secret (#5031).

A saved test capture is a file that tests read back as a vendor's answer or
as other recorded data: everything under ``tests/fixtures/`` and
``tests/data/`` that is not Python code. A capture is often recorded from a
real system, so it can carry a real token or password by mistake.

:func:`scan_text` looks for:

* the marker of the planted values of ``test_redaction_planted_secrets.py``
  (:data:`PLANTED_MARKER`), so a value planted by a test never ends up in a
  saved capture;
* common secret shapes: the Tier-1 credential patterns of the redaction
  library (``Authorization:`` header, ``Bearer`` token, JWT, labelled
  ``password=`` / ``token:`` pairs, BIND key secret), a private key block, an
  AWS access key id, and GitHub and Slack tokens;
* a field with a secret name and a value, as a quoted pair (JSON
  ``"password": "..."``, Python ``'password': "..."`` with either quote,
  escaped JSON ``\\"password\\": \\"...\\"``; found by the shared
  ``iter_quoted_secret_fields``), as an object or a list under a secret name
  (``"credentials": {"value": "..."}``: it counts when a string value inside
  looks like a secret), as YAML (``password: ...``) or as XML
  (``<password>...</password>``). The name rule is the shared
  ``is_secret_param_name``.

A value that is clearly not a secret is skipped: a ``<placeholder>``, a value
some step already masked (``[REDACTED:...]``, ``***``), a template reference
(``${...}``, ``{{ ... }}``), a URL with no user part, a value that is itself
the name of a secret field (``kubeconfig_field: kubeconfig``), and a value
shorter than 8 characters.

The ``kubeconfig`` pattern is not used: it also matches every Kubernetes
``ConfigMap`` in a rendered Helm chart. The redaction fixtures under
``tests/redaction_fixtures/`` are not scanned: they hold made-up secret
shapes on purpose, to test the redaction itself.

This file runs in the required unit test job, so a capture with a finding
blocks the merge. Fix a finding by replacing the value with a made-up one or
a ``<placeholder>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from meho_backplane.broadcast.events import is_secret_param_name
from meho_backplane.redaction.patterns import NAMED_PATTERNS, secret_spans
from meho_backplane.redaction.secret_fields import QuotedSecretField, iter_quoted_secret_fields

#: The start of every value ``test_redaction_planted_secrets.py`` plants.
PLANTED_MARKER: Final = "planted-secret-"

_TESTS_DIR = Path(__file__).parent
CAPTURE_DIRS: Final = (_TESTS_DIR / "fixtures", _TESTS_DIR / "data")

#: Tier-1 patterns that are real credential shapes in a saved file.
_TIER1_SHAPES: Final = (
    "authorization_header",
    "bearer_token",
    "jwt",
    "api_key",
    "bind_key_secret",
)

_OTHER_SHAPES: Final[dict[str, re.Pattern[str]]] = {
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "slack_token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
}

_NAME = r"(?P<name>[A-Za-z0-9_.\-]{1,128})"
_FIELD_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "yaml_field": re.compile(
        r"(?m)^[ \t-]*" + _NAME + r"[ \t]*:[ \t]+(?P<value>[^\s#][^\n]*?)[ \t]*$"
    ),
    "xml_field": re.compile(r"<(?:[\w.-]+:)?" + _NAME + r"(?:\s[^>]*)?>(?P<value>[^<]{1,4096})</"),
}

_NOT_A_SECRET: Final = re.compile(
    r"""
    <[^<>]*>                                # <placeholder>
    | \[(?:MEHO-)?(?:REDACTED|OMITTED)[^\]]*\]
    | \[redacted\]
    | \*+(?:REDACTED)?\*+                   # *** or ***REDACTED***
    | \$\{[^}]*\}                           # ${ENV_VAR}
    | \{\{.*\}\}                            # {{ template }}
    | https?://[^\s/@]+(?:/\S*)?            # a URL with no user part
    """,
    re.VERBOSE,
)

_MIN_VALUE_LENGTH: Final = 8

_FIELD_NAME: Final = re.compile(r"[A-Za-z_]+")

#: A quoted string inside an object or a list; ``colon`` is set when it is a key.
_QUOTED_STRING: Final = re.compile(
    r"""(?:"(?P<double>(?:[^"\\]|\\.)*)"|'(?P<single>(?:[^'\\]|\\.)*)')(?P<colon>\s*:)?"""
)


class Finding(NamedTuple):
    """One secret-like match in a capture."""

    path: str
    line: int
    rule: str


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _is_secret_value(value: str) -> bool:
    value = value.strip().strip("'\"`).,;")
    if len(value) < _MIN_VALUE_LENGTH:
        return False
    if _FIELD_NAME.fullmatch(value) and is_secret_param_name(value):
        return False  # it names a secret field (``kubeconfig_field: kubeconfig``)
    return _NOT_A_SECRET.fullmatch(value) is None


def _string_values_inside(field: QuotedSecretField) -> Iterator[str]:
    """The string values (not the keys) inside an object or a list value."""
    value = re.sub(r"\\(.)", r"\1", field.value) if field.escaped else field.value
    for match in _QUOTED_STRING.finditer(value):
        if match.group("colon") is None:
            yield (
                match.group("double")
                if match.group("double") is not None
                else match.group("single")
            )


def _holds_secret_value(field: QuotedSecretField) -> bool:
    if field.bracketed:
        return any(_is_secret_value(value) for value in _string_values_inside(field))
    return _is_secret_value(field.value)


def scan_text(text: str, *, path: str = "<text>") -> list[Finding]:
    """Return every planted marker, secret shape and secret-named field in *text*."""
    findings: list[Finding] = []
    for match in re.finditer(re.escape(PLANTED_MARKER), text, re.IGNORECASE):
        findings.append(Finding(path, _line_of(text, match.start()), "planted_marker"))
    for name in _TIER1_SHAPES:
        for start, end in secret_spans(NAMED_PATTERNS[name], text):
            secret = text[start:end]
            if name == "api_key":
                secret = re.split(r"[=:]", secret, maxsplit=1)[-1]
            if _is_secret_value(secret):
                findings.append(Finding(path, _line_of(text, start), name))
    for name, pattern in _OTHER_SHAPES.items():
        for match in pattern.finditer(text):
            findings.append(Finding(path, _line_of(text, match.start()), name))
    for field in iter_quoted_secret_fields(text):
        if _holds_secret_value(field):
            findings.append(Finding(path, _line_of(text, field.start), "quoted_field"))
    for name, pattern in _FIELD_PATTERNS.items():
        for match in pattern.finditer(text):
            if is_secret_param_name(match.group("name")) and _is_secret_value(match.group("value")):
                findings.append(Finding(path, _line_of(text, match.start()), name))
    return findings


def _capture_files(dirs: tuple[Path, ...]) -> Iterator[Path]:
    for directory in dirs:
        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix not in {".py", ".pyc"}
                and "__pycache__" not in path.parts
            ):
                yield path


def scan_captures(dirs: tuple[Path, ...] = CAPTURE_DIRS) -> list[Finding]:
    """Scan every saved capture under *dirs*."""
    findings: list[Finding] = []
    for path in _capture_files(dirs):
        text = path.read_text(encoding="utf-8", errors="replace")
        shown = (
            path.relative_to(_TESTS_DIR.parent) if path.is_relative_to(_TESTS_DIR.parent) else path
        )
        findings.extend(scan_text(text, path=str(shown)))
    return findings


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_capture_dirs_have_captures() -> None:
    """The scan has files to look at, so a pass is not empty."""
    assert any(True for _ in _capture_files(CAPTURE_DIRS))


def test_saved_captures_hold_no_planted_value_and_no_secret() -> None:
    """No saved test capture holds a planted value or a secret shape."""
    assert scan_captures() == []


# ---------------------------------------------------------------------------
# The check fails on a planted capture
# ---------------------------------------------------------------------------

# The made-up shapes are joined at run time, so this source file itself holds
# no secret shape for the repository's own secret hooks to flag.
_FAKE_JWT = ".".join(("ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiJtYWRlLXVwIn0", "bWFkZS11cA"))
_FAKE_KEY_BLOCK = "-----BEGIN RSA " + "PRIVATE KEY-----\nmade-up\n"
_FAKE_AWS_KEY_ID = "AK" + "IA" + "MADEUPMADEUP1234"


@pytest.mark.parametrize(
    ("file_name", "content", "rule"),
    [
        ("answer.json", '{"note": "planted-secret-token-made-up"}', "planted_marker"),
        ("answer.json", '{"session": "' + _FAKE_JWT + '"}', "jwt"),
        ("answer.txt", "Authorization: Bearer made-up-token-value-123", "authorization_header"),
        ("answer.json", '{"user": "u", "password": "made-up-pass-123"}', "quoted_field"),
        ("answer.json", '{"encryptedPassword": "made-up-encrypted-1"}', "quoted_field"),
        ("answer.py.txt", "{'clientSecret': 'made-up-client-secret'}", "quoted_field"),
        ("answer.py.txt", "{'note': \"it's\", 'password': \"made-up-it's-pass\"}", "quoted_field"),
        ("answer.json", '{"body": "{\\"password\\": \\"made-up-pass-123\\"}"}', "quoted_field"),
        ("answer.json", '{"password": "' + "x" * 5000 + '"}', "quoted_field"),
        ("answer.json", '{"credentials": {"value": "made-up-secret-1"}}', "quoted_field"),
        ("answer.json", '{"password": ["made-up-pass-123", "made-up-pass-456"]}', "quoted_field"),
        ("answer.py.txt", "{'credentials': {'value': \"made-up-it's-secret\"}}", "quoted_field"),
        (
            "answer.json",
            '{"body": "{\\"credentials\\": {\\"value\\": \\"made-up-secret-1\\"}}"}',
            "quoted_field",
        ),
        ("answer.yaml", "account:\n  sessionToken: made-up-session-token\n", "yaml_field"),
        ("answer.xml", "<login><password>made-up-pass-123</password></login>", "xml_field"),
        ("key.pem", _FAKE_KEY_BLOCK, "private_key_block"),
        ("answer.json", '{"id": "' + _FAKE_AWS_KEY_ID + '"}', "aws_access_key_id"),
    ],
)
def test_scan_finds_a_planted_capture(
    tmp_path: Path, file_name: str, content: str, rule: str
) -> None:
    """A capture with a planted value or secret makes the check fail."""
    (tmp_path / file_name).write_text(content, encoding="utf-8")
    rules = {finding.rule for finding in scan_captures((tmp_path,))}
    assert rule in rules


@pytest.mark.parametrize(
    "content",
    [
        '{"name": "item-1", "state": "ready", "token_ttl": 3600}',
        '{"password": "<password>", "token": "[REDACTED:param_name]", "secret": "***"}',
        "password: ${MADE_UP_ENV}\ntoken_url: https://login.example.invalid/token\n",
        "<key>key-vim.host.ScsiDisk-0000</key><password></password>",
        '{"secret_ref": "tenants/made-up/target", "pwd": "short"}',
        "credential_field: kubeconfig\nnote: set `password=<generated>`).\n",
        '{"credentials": {"username": "<user>", "password": "<password>"}, "secret": {}}',
        '{"password": ["[REDACTED:param_name]"], "credentials": {"kind": "token", "ttl": 60}}',
    ],
)
def test_scan_passes_a_clean_capture(tmp_path: Path, content: str) -> None:
    """Placeholders, masked values, references and non-secret names do not count."""
    (tmp_path / "answer.txt").write_text(content, encoding="utf-8")
    assert scan_captures((tmp_path,)) == []
