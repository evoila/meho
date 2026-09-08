# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PBM (Storage Policy Based Management) SOAP codec (#3494).

The vim25 codec :mod:`~meho_backplane.connectors.vmware_rest.soap` is a
single cohesive hand-rolled SOAP 1.1 codec for the ``urn:vim25`` surface on
``/sdk``; PBM is a *distinct* SOAP service (endpoint ``/pbm``, namespace
``urn:pbm``, its own ``PbmServiceInstance`` bootstrap), so its envelope
builders + response parsers live in this sibling module. It **reuses** the
vim25 codec's low-level helpers (``_this`` / ``_envelope`` / ``_xml_escape``
for building, ``_parse_returnval`` / ``_soap_val_to_json`` / ``_find_body`` /
``_find_child`` / ``_local`` for parsing) — they are namespace-agnostic
(local-name walks over ``defusedxml`` nodes) — and adds only the ``urn:pbm``
method wrapper and the tag-rule create-spec builder.

The wire shapes here are grounded on govmomi ``pbm`` (client/types/methods),
``govc storage.policy.create``, and the community ``vmware_vm_storage_policy``
Ansible module: namespace ``http://www.vmware.com/storage/tag``, property id
``com.vmware.storage.tag.<category>.property``, subprofile "Tag based
placement", ``category=REQUIREMENT``, ``resourceType=STORAGE``. The
polymorphic ``constraints`` / ``value`` / ``values`` slots carry the
``xsi:type`` discriminator vmomi requires on a subclass-in-base-slot; element
order follows the pbm-types.xsd sequences.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from defusedxml.ElementTree import fromstring

from meho_backplane.connectors.vmware_rest.soap import (
    _envelope,
    _find_body,
    _find_child,
    _local,
    _parse_returnval,
    _soap_val_to_json,
    _this,
    _xml_escape,
)

#: PBM SOAP namespace (the ``urn:pbm`` method element default ns).
_PBM_NS: Final = "urn:pbm"

#: PBM SOAP endpoint path on a vCenter (distinct from vim25's ``/sdk``).
PBM_PATH: Final = "/pbm"

#: The ``PbmServiceInstance`` singleton MoRef — the fixed bootstrap object
#: ``PbmRetrieveServiceContent`` is invoked on (type == moId == the literal).
PBM_SERVICE_INSTANCE_TYPE: Final = "PbmServiceInstance"
PBM_SERVICE_INSTANCE_MOID: Final = "ServiceInstance"

#: MoRef type of the ProfileManager (``PbmServiceInstanceContent.profileManager``)
#: every create/delete/retrieve method is invoked on.
PBM_PROFILE_MANAGER_TYPE: Final = "PbmProfileProfileManager"

#: The tag-based capability namespace + property-id template (govc / Ansible
#: reference). ``id`` of the capability's ``PbmCapabilityMetadataUniqueId`` is
#: the tag *category* name; the property instance id is the formatted string.
_PBM_TAG_NAMESPACE: Final = "http://www.vmware.com/storage/tag"
#: ``category`` value on a requirement storage policy.
_PBM_CATEGORY_REQUIREMENT: Final = "REQUIREMENT"
#: The only legal ``resourceType`` for a storage policy.
_PBM_RESOURCE_TYPE_STORAGE: Final = "STORAGE"


def pbm_tag_property_id(category_name: str) -> str:
    """The ``PbmCapabilityPropertyInstance.id`` for a tag rule on *category_name*.

    Mirrors the govc ``com.vmware.storage.tag.%s.property`` / Ansible
    ``format_tag_mob_id`` convention verbatim.
    """
    return f"com.vmware.storage.tag.{category_name}.property"


def _pbm_method(name: str, body: str) -> str:
    """A ``urn:pbm`` method element (default ns so children inherit it)."""
    return f'<{name} xmlns="{_PBM_NS}">{body}</{name}>'


def build_pbm_service_content_envelope() -> str:
    """``PbmServiceInstance.PbmRetrieveServiceContent`` — the bootstrap read.

    Returns the ``PbmServiceInstanceContent`` whose ``profileManager`` MoRef
    every create/delete/retrieve is invoked on. Authenticated by the
    already-established ``vmware_soap_session`` cookie on the pooled client.
    """
    return _envelope(
        "PbmRetrieveServiceContent",
        _pbm_method(
            "PbmRetrieveServiceContent",
            _this(PBM_SERVICE_INSTANCE_TYPE, PBM_SERVICE_INSTANCE_MOID),
        ),
    )


def build_pbm_create_envelope(
    profile_manager_moid: str,
    *,
    name: str,
    description: str,
    category_name: str,
    tag_names: Sequence[str],
) -> str:
    """``PbmProfileProfileManager.PbmCreate`` for a tag-based requirement policy.

    Builds the ``PbmCapabilityProfileCreateSpec`` for a single tag rule: a
    ``PbmCapabilitySubProfileConstraints`` (name "Tag based placement") whose
    one capability names the tag ``category`` (``PbmCapabilityMetadataUniqueId``
    ``{namespace, id=category}``) and constrains it to the given *tag_names*
    (a ``PbmCapabilityDiscreteSet`` of ``xsd:string`` values). The polymorphic
    ``constraints`` / ``value`` / ``values`` slots carry the ``xsi:type``
    discriminator vmomi requires on a subclass-in-base-slot (element order
    follows the pbm-types.xsd sequences: create-spec name/description/category/
    resourceType/constraints; property-instance id/value; unique-id
    namespace/id).
    """
    property_id = pbm_tag_property_id(category_name)
    values_xml = "".join(
        f'<values xsi:type="xsd:string">{_xml_escape(tag)}</values>' for tag in tag_names
    )
    create_spec = (
        f"<name>{_xml_escape(name)}</name>"
        f"<description>{_xml_escape(description)}</description>"
        f"<category>{_PBM_CATEGORY_REQUIREMENT}</category>"
        f"<resourceType><resourceType>{_PBM_RESOURCE_TYPE_STORAGE}</resourceType></resourceType>"
        '<constraints xsi:type="PbmCapabilitySubProfileConstraints">'
        "<subProfiles>"
        "<name>Tag based placement</name>"
        "<capability>"
        "<id>"
        f"<namespace>{_xml_escape(_PBM_TAG_NAMESPACE)}</namespace>"
        f"<id>{_xml_escape(category_name)}</id>"
        "</id>"
        "<constraint>"
        "<propertyInstance>"
        f"<id>{_xml_escape(property_id)}</id>"
        '<value xsi:type="PbmCapabilityDiscreteSet">'
        f"{values_xml}"
        "</value>"
        "</propertyInstance>"
        "</constraint>"
        "</capability>"
        "</subProfiles>"
        "</constraints>"
    )
    inner = (
        _this(PBM_PROFILE_MANAGER_TYPE, profile_manager_moid)
        + f"<createSpec>{create_spec}</createSpec>"
    )
    return _envelope("PbmCreate", _pbm_method("PbmCreate", inner))


def build_pbm_delete_envelope(profile_manager_moid: str, profile_ids: Sequence[str]) -> str:
    """``PbmProfileProfileManager.PbmDelete`` — remove one or more policies by id."""
    ids_xml = "".join(
        f"<profileId><uniqueId>{_xml_escape(pid)}</uniqueId></profileId>" for pid in profile_ids
    )
    inner = _this(PBM_PROFILE_MANAGER_TYPE, profile_manager_moid) + ids_xml
    return _envelope("PbmDelete", _pbm_method("PbmDelete", inner))


def build_pbm_retrieve_content_envelope(
    profile_manager_moid: str, profile_ids: Sequence[str]
) -> str:
    """``PbmProfileProfileManager.PbmRetrieveContent`` — read policies by id (read-back)."""
    ids_xml = "".join(
        f"<profileIds><uniqueId>{_xml_escape(pid)}</uniqueId></profileIds>" for pid in profile_ids
    )
    inner = _this(PBM_PROFILE_MANAGER_TYPE, profile_manager_moid) + ids_xml
    return _envelope("PbmRetrieveContent", _pbm_method("PbmRetrieveContent", inner))


def _parse_returnval_list(xml: str, method: str) -> list[Any]:
    """Codec rule 1 for an *array* return: collect **every** ``returnval`` child.

    :func:`_parse_returnval` returns only the first ``returnval`` — correct for
    the singular vim methods, wrong for PBM methods whose ``returnval`` is a
    sequence (``PbmDelete`` outcomes, ``PbmRetrieveContent`` profiles). Returns
    ``[]`` when the response carries none.
    """
    root = fromstring(xml)
    body = _find_body(root)
    if body is None:
        return []
    response = _find_child(body, f"{method}Response")
    if response is None:
        response = next(iter(body), None)
    if response is None:
        return []
    out: list[Any] = []
    for child in response:
        if _local(child.tag) == "returnval":
            out.append(_soap_val_to_json(child))
    return out


def parse_pbm_service_content(xml: str) -> dict[str, Any]:
    """Parse ``PbmRetrieveServiceContentResponse`` -> the ServiceContent dict.

    Carries ``profileManager`` (MoRef ``{type, value}``) among the other
    manager MoRefs; the caller reads ``profileManager.value``.
    """
    content = _parse_returnval(xml, "PbmRetrieveServiceContent")
    return content if isinstance(content, dict) else {}


def parse_pbm_profile_id(xml: str) -> str | None:
    """Parse ``PbmCreateResponse`` -> the created profile's ``uniqueId`` string.

    ``returnval`` is a ``PbmProfileId`` (``{"uniqueId": "<guid>"}``). Returns
    the id, or ``None`` when the response carries no usable id.
    """
    val = _parse_returnval(xml, "PbmCreate")
    if isinstance(val, dict):
        unique_id = val.get("uniqueId")
        if isinstance(unique_id, str) and unique_id:
            return unique_id
    return None


def parse_pbm_delete_outcomes(xml: str) -> list[dict[str, Any]]:
    """Parse ``PbmDeleteResponse`` -> a list of ``PbmProfileOperationOutcome`` dicts.

    Each carries ``profileId`` (``{"uniqueId"}``) and, on failure, a ``fault``.
    An empty list means every requested id was removed without a reported
    per-id fault.
    """
    return [row for row in _parse_returnval_list(xml, "PbmDelete") if isinstance(row, dict)]


def parse_pbm_profiles(xml: str) -> list[dict[str, Any]]:
    """Parse ``PbmRetrieveContentResponse`` -> a list of profile dicts.

    Each carries ``profileId`` (``{"uniqueId"}``), ``name``, ``description``,
    and the capability ``constraints`` — the read-back a create/delete verify
    reads for the policy's presence/absence and name.
    """
    return [
        row for row in _parse_returnval_list(xml, "PbmRetrieveContent") if isinstance(row, dict)
    ]
