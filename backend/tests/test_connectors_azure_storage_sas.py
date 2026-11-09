# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The user delegation SAS that ``azure.storage.blob.link.create`` signs (#4169).

Pure functions, no HTTP: the user delegation key answer is parsed, the
string-to-sign of Blob service version 2026-04-06 and later is built (28
lines, Microsoft's "Create a user delegation SAS"), and the signature is
HMAC-SHA256 of it under the decoded key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

import pytest

from meho_backplane.connectors.azure.storage_parse import StorageReadError
from meho_backplane.connectors.azure.storage_sas import (
    UserDelegationKey,
    blob_link,
    parse_user_delegation_key,
    sign_blob_read_sas,
    string_to_sign,
)

KEY_VALUE = base64.b64encode(b"made-up-delegation-key-for-tests-0123456789").decode()
KEY = UserDelegationKey(
    signed_oid="11111111-2222-3333-4444-555555555555",
    signed_tid="66666666-7777-8888-9999-000000000000",
    signed_start="2026-11-05T09:55:00Z",
    signed_expiry="2026-11-05T14:00:00Z",
    signed_service="b",
    signed_version="2026-10-06",
    value=KEY_VALUE,
)
START = datetime(2026, 11, 5, 9, 55, tzinfo=UTC)
EXPIRY = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
ARGS = {
    "account": "acct",
    "container": "iso",
    "blob": "esxi 8.iso",
    "start": START,
    "expiry": EXPIRY,
    "ip": "198.51.100.7",
    "version": "2026-10-06",
}

KEY_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<UserDelegationKey>
    <SignedOid>{KEY.signed_oid}</SignedOid>
    <SignedTid>{KEY.signed_tid}</SignedTid>
    <SignedStart>{KEY.signed_start}</SignedStart>
    <SignedExpiry>{KEY.signed_expiry}</SignedExpiry>
    <SignedService>b</SignedService>
    <SignedVersion>2026-10-06</SignedVersion>
    <Value>{KEY_VALUE}</Value>
</UserDelegationKey>"""


def test_string_to_sign_has_the_28_lines_of_version_2026_04_06() -> None:
    lines = string_to_sign(KEY, **ARGS).split("\n")

    assert len(lines) == 28
    assert lines[:4] == [
        "r",
        "2026-11-05T09:55:00Z",
        "2026-11-05T14:00:00Z",
        "/blob/acct/iso/esxi 8.iso",
    ]
    assert lines[4:10] == [
        KEY.signed_oid,
        KEY.signed_tid,
        KEY.signed_start,
        KEY.signed_expiry,
        "b",
        "2026-10-06",
    ]
    assert lines[10:15] == [""] * 5
    assert lines[15:20] == ["198.51.100.7", "https", "2026-10-06", "b", ""]
    assert lines[20:] == [""] * 8


def test_sig_is_hmac_sha256_of_the_string_under_the_decoded_key() -> None:
    sas = dict(parse_qsl(sign_blob_read_sas(KEY, **ARGS)))

    digest = hmac.new(
        base64.b64decode(KEY_VALUE), string_to_sign(KEY, **ARGS).encode("utf-8"), hashlib.sha256
    ).digest()
    assert sas["sig"] == base64.b64encode(digest).decode()
    assert {k: sas[k] for k in ("sp", "sr", "spr", "sip", "sv", "skv", "sks")} == {
        "sp": "r",
        "sr": "b",
        "spr": "https",
        "sip": "198.51.100.7",
        "sv": "2026-10-06",
        "skv": "2026-10-06",
        "sks": "b",
    }
    assert {k: sas[k] for k in ("skoid", "sktid", "skt", "ske")} == {
        "skoid": KEY.signed_oid,
        "sktid": KEY.signed_tid,
        "skt": KEY.signed_start,
        "ske": KEY.signed_expiry,
    }


def test_times_are_iso_utc_seconds() -> None:
    sas = dict(parse_qsl(sign_blob_read_sas(KEY, **ARGS)))

    assert sas["st"] == "2026-11-05T09:55:00Z"
    assert sas["se"] == "2026-11-05T14:00:00Z"


def test_sas_canonical_resource_keeps_the_raw_blob_name() -> None:
    lines = string_to_sign(KEY, **ARGS).split("\n")
    link = blob_link("acct.blob.core.windows.net", "iso", "esxi 8.iso", "sv=x&sig=y")

    assert lines[3] == "/blob/acct/iso/esxi 8.iso"
    parts = urlsplit(link)
    assert parts.scheme == "https"
    assert parts.netloc == "acct.blob.core.windows.net"
    assert parts.path == "/iso/esxi%208.iso"
    assert parts.query == "sv=x&sig=y"


def test_parse_user_delegation_key_reads_every_field() -> None:
    assert parse_user_delegation_key(KEY_XML) == KEY


def test_parse_user_delegation_key_names_a_missing_element() -> None:
    broken = KEY_XML.replace(f"<Value>{KEY_VALUE}</Value>", "")

    with pytest.raises(StorageReadError, match="Value"):
        parse_user_delegation_key(broken)


def test_parse_user_delegation_key_refuses_xml_that_is_not_xml() -> None:
    with pytest.raises(StorageReadError) as caught:
        parse_user_delegation_key("<html>Bad gateway")

    assert KEY_VALUE not in str(caught.value)


def test_key_repr_hides_the_value() -> None:
    assert KEY_VALUE not in repr(KEY)
    assert KEY_VALUE not in str(KEY)
