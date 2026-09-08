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
    env = build_pbm_service_content_envelope()
    fromstring(env)  # raises on malformed XML
    assert '<PbmRetrieveServiceContent xmlns="urn:pbm">' in env
    assert '<_this type="PbmServiceInstance">ServiceInstance</_this>' in env


def test_create_envelope_matches_the_tag_rule_reference_shape() -> None:
    env = build_pbm_create_envelope(
        "pm-1",
        name="NFS-Gold",
        description="NFS tag policy",
        category_name="meho-storage",
        tag_names=["nfs-gold", "nfs-silver"],
    )
    fromstring(env)
    # PbmCreate on the ProfileManager, urn:pbm.
    assert '<PbmCreate xmlns="urn:pbm">' in env
    assert '<_this type="PbmProfileProfileManager">pm-1</_this>' in env
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
    )
    fromstring(env)
    assert "a&amp;b" in env
    assert "&lt;desc&gt;" in env
    assert "com.vmware.storage.tag.c&amp;c.property" in env


def test_property_id_helper_matches_govc_convention() -> None:
    assert pbm_tag_property_id("my_cat") == "com.vmware.storage.tag.my_cat.property"


def test_delete_and_retrieve_envelopes_carry_profile_ids() -> None:
    del_env = build_pbm_delete_envelope("pm-1", ["p-1", "p-2"])
    fromstring(del_env)
    assert '<PbmDelete xmlns="urn:pbm">' in del_env
    assert "<profileId><uniqueId>p-1</uniqueId></profileId>" in del_env
    assert "<profileId><uniqueId>p-2</uniqueId></profileId>" in del_env

    ret_env = build_pbm_retrieve_content_envelope("pm-1", ["p-1"])
    fromstring(ret_env)
    assert '<PbmRetrieveContent xmlns="urn:pbm">' in ret_env
    assert "<profileIds><uniqueId>p-1</uniqueId></profileIds>" in ret_env


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
