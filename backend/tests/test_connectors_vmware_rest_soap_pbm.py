# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the PBM SOAP codec (``vmware_rest/soap_pbm.py``, #3494).

Storage-policy creation has no vCenter REST path, so it rides the PBM SOAP
API. This module is the single point where the hand-rolled ``urn:pbm``
envelopes and their response parsers can silently drift from the wire shape
real vCenter accepts (the mock-vs-hardware trap the vim25 codec docstring
warns about), so every builder is asserted against the govmomi / govc /
Ansible reference shape (namespace, property-id, subprofile name, xsi:type
discriminators, element order) and every parser is round-tripped on a
synthetic ``urn:pbm`` response envelope.
"""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from meho_backplane.connectors.vmware_rest.soap_pbm import (
    build_pbm_create_envelope,
    build_pbm_delete_envelope,
    build_pbm_retrieve_content_envelope,
    build_pbm_service_content_envelope,
    parse_pbm_delete_outcomes,
    parse_pbm_profile_id,
    parse_pbm_profile_tag_rules,
    parse_pbm_profiles,
    parse_pbm_service_content,
    pbm_tag_property_id,
)

_SOAPENV = "http://schemas.xmlsoap.org/soap/envelope/"


def _body(inner: str) -> str:
    """Wrap *inner* (a ``{Method}Response`` element) in a SOAP envelope."""
    return (
        f'<soapenv:Envelope xmlns:soapenv="{_SOAPENV}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


# --- Builders --------------------------------------------------------------


def test_service_content_envelope_is_well_formed_and_targets_pbm_service_instance() -> None:
    env = build_pbm_service_content_envelope("session-key-1")
    fromstring(env)  # raises on malformed XML
    assert '<PbmRetrieveServiceContent xmlns="urn:pbm">' in env
    assert '<_this type="PbmServiceInstance">ServiceInstance</_this>' in env
    # Even the lenient bootstrap carries the vcSessionCookie auth header (#3810).
    assert (
        "<soapenv:Header><vcSessionCookie>session-key-1</vcSessionCookie></soapenv:Header>" in env
    )


def test_create_envelope_matches_the_tag_rule_reference_shape() -> None:
    env = build_pbm_create_envelope(
        "pm-1",
        name="NFS-Gold",
        description="NFS tag policy",
        category_name="meho-storage",
        tag_names=["nfs-gold", "nfs-silver"],
        session_cookie="session-key-1",
    )
    fromstring(env)
    # PbmCreate on the ProfileManager, urn:pbm.
    assert '<PbmCreate xmlns="urn:pbm">' in env
    assert '<_this type="PbmProfileProfileManager">pm-1</_this>' in env
    # The vcSessionCookie SOAP header authenticates the write on /pbm (#3810).
    assert (
        "<soapenv:Header><vcSessionCookie>session-key-1</vcSessionCookie></soapenv:Header>" in env
    )
    # Requirement storage policy.
    assert "<category>REQUIREMENT</category>" in env
    assert "<resourceType><resourceType>STORAGE</resourceType></resourceType>" in env
    # The polymorphic constraints slot carries the xsi:type discriminator.
    assert '<constraints xsi:type="PbmCapabilitySubProfileConstraints">' in env
    assert "<name>Tag based placement</name>" in env
    # Tag capability: namespace + category id + property id (govc / Ansible).
    assert "<namespace>http://www.vmware.com/storage/tag</namespace>" in env
    assert "<id>meho-storage</id>" in env
    assert "<id>com.vmware.storage.tag.meho-storage.property</id>" in env
    # DiscreteSet of xsd:string values, one per tag.
    assert '<value xsi:type="PbmCapabilityDiscreteSet">' in env
    assert '<values xsi:type="xsd:string">nfs-gold</values>' in env
    assert '<values xsi:type="xsd:string">nfs-silver</values>' in env


def test_create_envelope_xml_escapes_names() -> None:
    env = build_pbm_create_envelope(
        "pm-1",
        name="a&b",
        description="<desc>",
        category_name="c&c",
        tag_names=["t<1"],
        session_cookie="cookie&<value>",
    )
    fromstring(env)
    assert "a&amp;b" in env
    assert "&lt;desc&gt;" in env
    assert "com.vmware.storage.tag.c&amp;c.property" in env
    # The session cookie value is XML-escaped in the header, never raw.
    assert "<vcSessionCookie>cookie&amp;&lt;value&gt;</vcSessionCookie>" in env


def test_property_id_helper_matches_govc_convention() -> None:
    assert pbm_tag_property_id("my_cat") == "com.vmware.storage.tag.my_cat.property"


def test_delete_and_retrieve_envelopes_carry_profile_ids() -> None:
    del_env = build_pbm_delete_envelope("pm-1", ["p-1", "p-2"], session_cookie="session-key-1")
    fromstring(del_env)
    assert '<PbmDelete xmlns="urn:pbm">' in del_env
    assert "<profileId><uniqueId>p-1</uniqueId></profileId>" in del_env
    assert "<profileId><uniqueId>p-2</uniqueId></profileId>" in del_env
    assert (
        "<soapenv:Header><vcSessionCookie>session-key-1</vcSessionCookie></soapenv:Header>"
        in del_env
    )

    ret_env = build_pbm_retrieve_content_envelope("pm-1", ["p-1"], session_cookie="session-key-1")
    fromstring(ret_env)
    assert '<PbmRetrieveContent xmlns="urn:pbm">' in ret_env
    assert "<profileIds><uniqueId>p-1</uniqueId></profileIds>" in ret_env
    assert (
        "<soapenv:Header><vcSessionCookie>session-key-1</vcSessionCookie></soapenv:Header>"
        in ret_env
    )


# --- Parsers ---------------------------------------------------------------


def test_parse_service_content_extracts_profile_manager_moref() -> None:
    xml = _body(
        '<PbmRetrieveServiceContentResponse xmlns="urn:pbm"><returnval>'
        "<aboutInfo><name>PBM</name></aboutInfo>"
        '<sessionManager type="PbmSessionManager">SessionManager</sessionManager>'
        '<profileManager type="PbmProfileProfileManager">ProfileManager</profileManager>'
        "</returnval></PbmRetrieveServiceContentResponse>"
    )
    content = parse_pbm_service_content(xml)
    assert content["profileManager"] == {
        "type": "PbmProfileProfileManager",
        "value": "ProfileManager",
    }


def test_parse_profile_id_returns_unique_id() -> None:
    xml = _body(
        '<PbmCreateResponse xmlns="urn:pbm"><returnval>'
        "<uniqueId>abc-123-guid</uniqueId></returnval></PbmCreateResponse>"
    )
    assert parse_pbm_profile_id(xml) == "abc-123-guid"


def test_parse_profile_id_none_when_absent() -> None:
    xml = _body('<PbmCreateResponse xmlns="urn:pbm"/>')
    assert parse_pbm_profile_id(xml) is None


def test_parse_delete_outcomes_collects_every_returnval() -> None:
    xml = _body(
        '<PbmDeleteResponse xmlns="urn:pbm">'
        "<returnval><profileId><uniqueId>p-1</uniqueId></profileId></returnval>"
        "<returnval><profileId><uniqueId>p-2</uniqueId></profileId>"
        '<fault xsi:type="PbmFault"><localizedMessage>in use</localizedMessage></fault>'
        "</returnval>"
        "</PbmDeleteResponse>"
    )
    outcomes = parse_pbm_delete_outcomes(xml)
    assert [o["profileId"]["uniqueId"] for o in outcomes] == ["p-1", "p-2"]
    assert outcomes[0].get("fault") is None
    assert outcomes[1]["fault"]["_typeName"] == "PbmFault"


def test_parse_delete_outcomes_empty_on_no_returnval() -> None:
    xml = _body('<PbmDeleteResponse xmlns="urn:pbm"/>')
    assert parse_pbm_delete_outcomes(xml) == []


def test_parse_profiles_lists_content_rows() -> None:
    xml = _body(
        '<PbmRetrieveContentResponse xmlns="urn:pbm">'
        '<returnval xsi:type="PbmCapabilityProfile">'
        "<profileId><uniqueId>abc-123-guid</uniqueId></profileId>"
        "<name>NFS-Gold</name><description>d</description></returnval>"
        "</PbmRetrieveContentResponse>"
    )
    profiles = parse_pbm_profiles(xml)
    assert len(profiles) == 1
    assert profiles[0]["profileId"]["uniqueId"] == "abc-123-guid"
    assert profiles[0]["name"] == "NFS-Gold"


def _profile_with_tag_rule(unique_id: str, category: str, *tags: str) -> str:
    """A ``returnval`` carrying one tag rule constraining *category* to *tags*.

    Mirrors the shape :func:`build_pbm_create_envelope` writes (subprofile
    "Tag based placement", the ``com.vmware.storage.tag.<category>.property``
    property id, a ``PbmCapabilityDiscreteSet`` of ``xsd:string`` values) so the
    #3826 adopt-vs-conflict signature extraction is tested against the real wire
    shape, not a convenient one.
    """
    values = "".join(f'<values xsi:type="xsd:string">{t}</values>' for t in tags)
    return (
        '<returnval xsi:type="PbmCapabilityProfile">'
        f"<profileId><uniqueId>{unique_id}</uniqueId></profileId>"
        f"<name>{unique_id}-name</name>"
        '<constraints xsi:type="PbmCapabilitySubProfileConstraints">'
        "<subProfiles><name>Tag based placement</name><capability>"
        f"<id><namespace>http://www.vmware.com/storage/tag</namespace><id>{category}</id></id>"
        "<constraint><propertyInstance>"
        f"<id>{pbm_tag_property_id(category)}</id>"
        f'<value xsi:type="PbmCapabilityDiscreteSet">{values}</value>'
        "</propertyInstance></constraint>"
        "</capability></subProfiles></constraints>"
        "</returnval>"
    )


def test_parse_profile_tag_rules_extracts_per_profile_signatures() -> None:
    xml = _body(
        '<PbmRetrieveContentResponse xmlns="urn:pbm">'
        + _profile_with_tag_rule("guid-1", "meho-storage", "nfs-gold")
        + _profile_with_tag_rule("guid-2", "meho-storage", "nfs-gold", "nfs-silver")
        + "</PbmRetrieveContentResponse>"
    )
    rules = parse_pbm_profile_tag_rules(xml)
    assert rules == {
        "guid-1": {(pbm_tag_property_id("meho-storage"), frozenset({"nfs-gold"}))},
        "guid-2": {(pbm_tag_property_id("meho-storage"), frozenset({"nfs-gold", "nfs-silver"}))},
    }


def test_parse_profile_tag_rules_empty_for_profile_without_tag_rule() -> None:
    xml = _body(
        '<PbmRetrieveContentResponse xmlns="urn:pbm">'
        '<returnval xsi:type="PbmCapabilityProfile">'
        "<profileId><uniqueId>guid-3</uniqueId></profileId><name>bare</name>"
        "</returnval>"
        "</PbmRetrieveContentResponse>"
    )
    assert parse_pbm_profile_tag_rules(xml) == {"guid-3": set()}


def test_parse_profile_tag_rules_empty_on_no_returnval() -> None:
    xml = _body('<PbmRetrieveContentResponse xmlns="urn:pbm"/>')
    assert parse_pbm_profile_tag_rules(xml) == {}
