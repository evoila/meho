# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret parts of a signed download link stay hidden.

A link passed as an operation param shows up in the approval request
(the params echo) and in the flight recorder bodies. Both hide the user
part of the link and the values of its signature, credential and token
query parameters, and keep the rest of the link readable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.flight_recorder._capture_util import safe_url
from meho_backplane.operations._preview import PreviewContext, build_proposed_effect
from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.patterns import NAMED_PATTERNS, secret_spans
from meho_backplane.redaction.url_secret import is_secret_query_name

# Made-up values only. Each one must be gone from every output.
_S3_SIGNATURE: Final = "made-up-s3-signature-0a1b2c3d"
_S3_CREDENTIAL: Final = "MADEUPKEYID%2F20261105%2Fregion-1%2Fs3%2Faws4_request"
_AZURE_SIG: Final = "made-up-azure-sig%2Babc%3D"
_GOOGLE_SIGNATURE: Final = "made-up-google-signature-99ff"
_GOOGLE_CREDENTIAL: Final = "made-up-account%40project.iam"
_PASSWORD: Final = "made-up-password-7"
_TOKEN: Final = "made-up-token-value-12345"
_SECURITY_TOKEN: Final = "made-up-security-token-6789"
_QUOTE_PASSWORD: Final = "made-up-pass'word-8"
_CLUSTER_LINK: Final = f"https://admin:{_PASSWORD}@cluster.example.com"  # trufflehog:ignore

#: (link, secret values in it, readable parts that must stay)
_LINKS: Final[list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = [
    (
        "https://bucket.files.example.com/images/installer.iso"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={_S3_CREDENTIAL}"
        "&X-Amz-Date=20261105T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
        f"&X-Amz-Signature={_S3_SIGNATURE}",
        (_S3_SIGNATURE, _S3_CREDENTIAL),
        ("bucket.files.example.com/images/installer.iso", "X-Amz-Expires=3600"),
    ),
    (
        "https://account.files.example.com/media/disk.ova"
        f"?sv=2022-11-02&se=2026-12-01T00%3A00%3A00Z&sr=b&sp=r&sig={_AZURE_SIG}",
        (_AZURE_SIG,),
        ("account.files.example.com/media/disk.ova", "sp=r"),
    ),
    (
        "https://storage.files.example.com/bucket/package.zip"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
        f"&X-Goog-Credential={_GOOGLE_CREDENTIAL}"
        "&X-Goog-Date=20261105T000000Z&X-Goog-Expires=600&X-Goog-SignedHeaders=host"
        f"&X-Goog-Signature={_GOOGLE_SIGNATURE}",
        (_GOOGLE_SIGNATURE, _GOOGLE_CREDENTIAL),
        ("storage.files.example.com/bucket/package.zip", "X-Goog-Expires=600"),
    ),
    (
        f"https://deploy:{_PASSWORD}@files.example.com/packages/ipam.zip",  # trufflehog:ignore
        (_PASSWORD,),
        ("@files.example.com/packages/ipam.zip",),
    ),
    (
        f"https://files.example.com/download?token={_TOKEN}",
        (_TOKEN,),
        ("files.example.com/download",),
    ),
    (
        "https://files.example.com/download"
        f"?X-Amz-Security-Token={_SECURITY_TOKEN}&X-Amz-Signature={_S3_SIGNATURE}",
        (_SECURITY_TOKEN, _S3_SIGNATURE),
        ("files.example.com/download",),
    ),
    # An apostrophe is a valid URL byte: in the path, in a query value and in a password.
    (
        f"https://deploy:{_QUOTE_PASSWORD}@files.example.com"  # trufflehog:ignore
        f"/it's/installer.iso?note=o'clock&sig={_AZURE_SIG}",
        (_QUOTE_PASSWORD, _AZURE_SIG),
        ("@files.example.com/it's/installer.iso", "note=o'clock"),
    ),
]

_LINK_IDS: Final = [
    "s3",
    "azure-sas",
    "google",
    "user-password",
    "token",
    "security-token",
    "apostrophes",
]


def _operator() -> Operator:
    return Operator(
        sub="op-url-secret-test",
        name="URL Secret Test Operator",
        email=None,
        raw_jwt="op.url.jwt",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000b0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _FakeDescriptor:
    op_id: str


class _FakeTarget:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.name = "target-a"


def _assert_hidden(text: str, secrets: tuple[str, ...], readable: tuple[str, ...]) -> None:
    for secret in secrets:
        assert secret not in text
    for part in readable:
        assert part in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("link", "secrets", "readable"), _LINKS, ids=_LINK_IDS)
async def test_approval_request_hides_the_secret_parts_of_a_link(
    link: str, secrets: tuple[str, ...], readable: tuple[str, ...]
) -> None:
    ctx = PreviewContext(
        descriptor=_FakeDescriptor(op_id="files.download"),  # type: ignore[arg-type]
        connector_instance=None,
        operator=_operator(),
        target=_FakeTarget(),  # type: ignore[arg-type]
        connector_id="files-1.x",
        params={"url": link, "nested": {"links": [link]}},
    )
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    echo = effect["params_echo"]
    _assert_hidden(echo["url"], secrets, readable)
    _assert_hidden(echo["nested"]["links"][0], secrets, readable)


@pytest.mark.parametrize(("link", "secrets", "readable"), _LINKS, ids=_LINK_IDS)
def test_flight_recorder_body_hides_the_secret_parts_of_a_link(
    link: str, secrets: tuple[str, ...], readable: tuple[str, ...]
) -> None:
    body, fired = scrub_content({"source": {"url": link}})
    assert fired
    _assert_hidden(body["source"]["url"], secrets, readable)

    text, fired = scrub_content(f'download "{link}" to the datastore')
    assert fired
    _assert_hidden(text, secrets, readable)


def test_token_link_keeps_its_api_key_label() -> None:
    body, _ = scrub_content(f"https://files.example.com/download?token={_TOKEN}")
    assert body == "https://files.example.com/download?[REDACTED:api_key]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Only the secret values go; names, host and other params stay.
        (
            "https://h.example.com/f?sig=abc&sp=r#part",
            "https://h.example.com/f?sig=[REDACTED:url_secret]&sp=r#part",
        ),
        # A user part without a password is hidden whole (a token as user name).
        (
            "https://made-up-user-token@h.example.com/repo",
            "https://[REDACTED:url_secret]@h.example.com/repo",
        ),
        # ``&amp;`` as written in HTML and XML text.
        (
            "<a href='https://h.example.com/f?a=1&amp;Signature=abc'>",
            "<a href='https://h.example.com/f?a=1&amp;Signature=[REDACTED:url_secret]'>",
        ),
        # An encoded parameter name is read decoded.
        (
            "https://h.example.com/f?X%2DAmz%2DSignature=abc",
            "https://h.example.com/f?X%2DAmz%2DSignature=[REDACTED:url_secret]",
        ),
        # An ``@`` in a query value is not a user part.
        (
            "https://h.example.com?owner=a@b.example.com&sig=abc",
            "https://h.example.com?owner=a@b.example.com&sig=[REDACTED:url_secret]",
        ),
        # An apostrophe in the path or in a value does not end the link.
        (
            "get https://h.example.com/it's/f?note=o'clock&sig=abc now",
            "get https://h.example.com/it's/f?note=o'clock&sig=[REDACTED:url_secret] now",
        ),
        (
            "https://user:it's@h.example.com/f",
            "https://[REDACTED:url_secret]@h.example.com/f",
        ),
        # A link in single quotes ends at its closing quote.
        (
            "<a href='https://h.example.com/f?sig=abc'>",
            "<a href='https://h.example.com/f?sig=[REDACTED:url_secret]'>",
        ),
        (
            "{'url': 'https://h.example.com/it's/f?sig=abc', 'name': 'f'}",
            "{'url': 'https://h.example.com/it's/f?sig=[REDACTED:url_secret]', 'name': 'f'}",
        ),
        (
            "'https://h.example.com/f?sig=abc'.",
            "'https://h.example.com/f?sig=[REDACTED:url_secret]'.",
        ),
        # A link in double quotes keeps its apostrophes.
        (
            '"https://h.example.com/it\'s/f?sig=abc"',
            '"https://h.example.com/it\'s/f?sig=[REDACTED:url_secret]"',
        ),
    ],
)
def test_url_secret_hides_only_the_secret_parts(text: str, expected: str) -> None:
    pattern = NAMED_PATTERNS["url_secret"]
    out = text
    for start, end in reversed(secret_spans(pattern, text)):
        out = out[:start] + "[REDACTED:url_secret]" + out[end:]
    assert out == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no link here, sig=abc",
        "https://h.example.com/f?sig=&sp=r",
        "https://h.example.com/f?sp=r&se=2026-12-01&X-Amz-Expires=600",
        "https://h.example.com/f#sig=abc",
        "https://h.example.com/f?sig=[REDACTED:api_key]",
        "https://@h.example.com/f",
        "a.b." * 5000 + "c",
    ],
)
def test_url_secret_leaves_other_text_alone(text: str) -> None:
    assert secret_spans(NAMED_PATTERNS["url_secret"], text) == []


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("X-Amz-Signature", True),
        ("X-Amz-Credential", True),
        ("X-Goog-Signature", True),
        ("sig", True),
        ("Signature", True),
        ("accessToken", True),
        ("api_key", True),
        ("X-Amz-SignedHeaders", False),
        ("X-Amz-Expires", False),
        ("sp", False),
        ("signed", False),
        ("", False),
        ("-", False),
    ],
)
def test_is_secret_query_name(name: str, secret: bool) -> None:
    assert is_secret_query_name(name) is secret


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"{_CLUSTER_LINK}:6443/api/v1/pods?token={_TOKEN}#x",
            "https://cluster.example.com:6443/api/v1/pods",
        ),
        (_CLUSTER_LINK, "https://cluster.example.com"),
        ("https://cluster.example.com?owner=a@b.example.com", "https://cluster.example.com"),
        ("https://cluster.example.com/a@b", "https://cluster.example.com/a@b"),
        ("/api/v1/pods?token=abc", "/api/v1/pods"),
    ],
)
def test_safe_url_drops_the_user_part_of_a_plain_string(url: str, expected: str) -> None:
    assert safe_url(url) == expected
