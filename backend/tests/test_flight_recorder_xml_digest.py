# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Flight-recorder XML fault digest tests (Task #3708).

An XML body (a vim25 SOAP answer) is never recorded, but the trace keeps a
digest in its place: the omitted marker, a SHA-256 of the captured bytes,
and the count of each fault type name. These tests check that the fault
types show up, that fault text and secrets never do, and that the trace
stays operator-only.

All secrets here are synthetic and share the ``PLACEHOLDER`` sentinel.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.flight_recorder._capture_util import MAX_SPAN_BODY_BYTES, cap_body
from meho_backplane.redaction.flight_recorder import (
    BODY_OMITTED_MARKER,
    redact_body,
    redact_span,
)
from meho_backplane.settings import get_settings

_SENTINEL = "PLACEHOLDER"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings env so the op-family classifier in ``redact_span`` resolves."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_ENVELOPE_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<soapenv:Body>"
)
_ENVELOPE_TAIL = "</soapenv:Body></soapenv:Envelope>"


def _missing(path: str, fault_type: str | None) -> str:
    if fault_type is None:
        return f"<missingSet><path>{path}</path></missingSet>"
    return (
        f"<missingSet><path>{path}</path>"
        '<fault xsi:type="LocalizedMethodFault">'
        f'<fault xsi:type="{fault_type}">'
        '<object type="HostSystem">ha-host</object>'
        "<privilegeId>PLACEHOLDER-privilege</privilegeId>"
        "</fault>"
        "<localizedMessage>PLACEHOLDER fault text echoing a value</localizedMessage>"
        "</fault></missingSet>"
    )


def _retrieve_response(missing: str) -> str:
    return (
        _ENVELOPE_HEAD
        + '<RetrievePropertiesExResponse xmlns="urn:vim25"><returnval><objects>'
        + '<obj type="HostSystem">ha-host</obj>'
        + '<propSet><name>name</name><val xsi:type="xsd:string">esx-a</val></propSet>'
        + missing
        + "</objects></returnval></RetrievePropertiesExResponse>"
        + _ENVELOPE_TAIL
    )


_MIXED_MISSING_SET = _retrieve_response(
    _missing("config.storageDevice", "NoPermission")
    + _missing("config.network", "NoPermission")
    + _missing("config.bogus", "InvalidProperty")
    + _missing("summary.customValue", None)
)

_TOP_LEVEL_FAULT = (
    _ENVELOPE_HEAD
    + "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
    + "<faultstring>PLACEHOLDER session text</faultstring>"
    + '<detail><NotAuthenticatedFault xmlns="urn:vim25" xsi:type="NotAuthenticated">'
    + "<object>PLACEHOLDER-object</object><privilegeId>System.View</privilegeId>"
    + "</NotAuthenticatedFault></detail></soapenv:Fault>"
    + _ENVELOPE_TAIL
)


def _assert_no_secret(obj: Any) -> None:
    blob = repr(obj)
    assert _SENTINEL not in blob, f"fault text or secret survived in {blob!r}"
    assert "localizedMessage" not in blob
    assert "faultstring" not in blob


def test_mixed_missing_set_yields_fault_type_names_and_counts() -> None:
    out = redact_body(_MIXED_MISSING_SET, content_type="text/xml; charset=utf-8")

    assert out.uncertain is True
    assert out.value == {
        "omitted": BODY_OMITTED_MARKER,
        "content_sha256": hashlib.sha256(_MIXED_MISSING_SET.encode("utf-8")).hexdigest(),
        "fault_types": {"InvalidProperty": 1, "NoPermission": 2},
    }
    assert "digest" in " ".join(out.reasons)
    _assert_no_secret(out.value)


def test_missing_set_without_fault_counts_no_fault_type() -> None:
    body = _retrieve_response(_missing("summary.customValue", None))
    out = redact_body(body.encode("utf-8"), content_type="text/xml")

    assert out.uncertain is True
    assert out.value["fault_types"] == {}
    assert out.value["omitted"] == BODY_OMITTED_MARKER


def test_top_level_soap_fault_type_is_counted() -> None:
    out = redact_body(_TOP_LEVEL_FAULT, content_type="text/xml")

    assert out.value["fault_types"] == {"NotAuthenticated": 1}
    _assert_no_secret(out.value)


def test_top_level_soap_fault_without_xsi_type_uses_element_name() -> None:
    body = _TOP_LEVEL_FAULT.replace(' xsi:type="NotAuthenticated"', "")
    out = redact_body(body, content_type="text/xml")

    assert out.value["fault_types"] == {"NotAuthenticatedFault": 1}


@pytest.mark.parametrize("content_type", ["application/xml", "application/soap+xml"])
def test_other_xml_content_types_get_a_digest(content_type: str) -> None:
    out = redact_body(_MIXED_MISSING_SET, content_type=content_type)

    assert out.uncertain is True
    assert out.value["fault_types"] == {"InvalidProperty": 1, "NoPermission": 2}


def test_type_name_that_is_not_an_identifier_is_dropped() -> None:
    body = _retrieve_response(_missing("config.a", "PLACEHOLDER secret text"))
    out = redact_body(body, content_type="text/xml")

    assert out.value["fault_types"] == {}
    _assert_no_secret(out.value)


def test_xml_body_with_a_secret_never_records_the_secret() -> None:
    body = (
        _ENVELOPE_HEAD
        + "<Login><userName>root</userName><password>PLACEHOLDER-pw123456</password></Login>"
        + _ENVELOPE_TAIL
    )
    out = redact_body(body, content_type="text/xml")

    assert out.uncertain is True
    assert out.value["fault_types"] == {}
    _assert_no_secret(out.value)


@pytest.mark.parametrize(
    "body",
    [
        "<not-closed><fault xsi:type='x' ",
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "PLACEHOLDER">]><x>&a;</x>',
        b"\xff\xfe\x00PLACEHOLDER",
    ],
)
def test_broken_or_unsafe_xml_keeps_hash_and_never_raises(body: str | bytes) -> None:
    out = redact_body(body, content_type="text/xml")

    assert out.uncertain is True
    assert out.value["fault_types"] == {}
    raw = body.encode("utf-8") if isinstance(body, str) else body
    assert out.value["content_sha256"] == hashlib.sha256(raw).hexdigest()
    _assert_no_secret(out.value)


def test_truncated_xml_counts_the_faults_before_the_cut() -> None:
    missing = "".join(_missing(f"config.p{i}", "NoPermission") for i in range(400))
    body = _retrieve_response(missing)
    capped, truncated = cap_body(body.encode("utf-8"))
    assert truncated is True
    assert len(capped) == MAX_SPAN_BODY_BYTES

    out = redact_body(capped, content_type="text/xml", truncated=True)

    assert out.uncertain is True
    assert 0 < out.value["fault_types"]["NoPermission"] < 400
    assert "truncated" in " ".join(out.reasons)


def test_span_records_the_digest_and_stays_operator_only() -> None:
    span = redact_span(
        op_id="vmware.object.collect",
        method="POST",
        response_body=_MIXED_MISSING_SET.encode("utf-8"),
        response_content_type="text/xml; charset=utf-8",
    )

    assert span.uncertain is True
    assert span.body_recorded is True
    assert span.response_body["fault_types"] == {"InvalidProperty": 1, "NoPermission": 2}
    assert span.response_body["omitted"] == BODY_OMITTED_MARKER
    _assert_no_secret(span.response_body)
