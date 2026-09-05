# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the hand-rolled ESXi SOAP codec (``vmware_rest/soap.py``, #3363).

The deserialiser is the single point where parity with the VI-JSON path
can silently break (the mock-vs-hardware trap that let #3332 ship green),
so the codec acceptance-spec checklist (rules 1-8) is the fixture matrix
here: one named test per shape, plus native-primitive-typing coverage,
the builder serialisers, and fault extraction. Every deserialiser fixture
is fed through the **unchanged** downstream consumers
(``unwrap_vim_value``, ``_extract_host_props``, ``_map_scsi_lun``) to
prove the SOAP branch produces the exact dict shapes those consumers
already read -- the parity guarantee the ticket gates the "works" claim
on.

Envelopes are realistic hand-written vim25 shapes (modelled on the WSDL),
not live captures -- the committed live-envelope fixtures land with the
State-2 run.
"""

from __future__ import annotations

from typing import Any

from defusedxml.ElementTree import fromstring

from meho_backplane.connectors.vmware_rest.soap import (
    SoapFault,
    _coerce_leaf,
    _soap_val_to_json,
    build_create_nas_datastore_envelope,
    build_login_envelope,
    build_logout_envelope,
    build_mark_ssd_envelope,
    build_query_boot_devices_envelope,
    build_retrieve_properties_ex_envelope,
    build_service_content_envelope,
    parse_boot_devices,
    parse_moref_result,
    parse_retrieve_result,
    parse_service_content,
    parse_soap_fault,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
    _extract_host_props,
    _map_scsi_lun,
    _moref_value,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    retrieve_properties_body,
    unwrap_vim_value,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _val_of(xml_fragment: str) -> Any:
    """Parse a bare XML fragment and run it through the deserialiser core."""
    return _soap_val_to_json(fromstring(xml_fragment))


def _envelope(body_inner: str) -> str:
    """Wrap a Body inner fragment in a minimal SOAP 1.1 envelope."""
    return (
        "<soapenv:Envelope "
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        f"<soapenv:Body>{body_inner}</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


# A live-shaped RetrievePropertiesEx result for one HostSystem carrying the
# scsiLun array (a HostScsiDisk with bare boolean flags + int capacity, plus
# a non-disk ScsiLun) and the bootDeviceSystem MoRef property. Leaf text with
# meaningful trailing padding (model / vendor) is written inline so the
# padding is not a formatting artefact.
_SCSI_LUN_RETRIEVE_XML = _envelope(
    """<RetrievePropertiesExResponse xmlns="urn:vim25"><returnval>
    <objects>
      <obj type="HostSystem">ha-host</obj>
      <propSet>
        <name>config.storageDevice.scsiLun</name>
        <val xsi:type="ArrayOfScsiLun">
          <ScsiLun xsi:type="HostScsiDisk">
            <uuid>0200000000600a</uuid>
            <canonicalName>naa.6000c290</canonicalName>
            <deviceType>disk</deviceType>
            <ssd>true</ssd>
            <localDisk>true</localDisk>
            <model>Virtual disk   </model>
            <vendor>VMware  </vendor>
            <capacity><blockSize>512</blockSize><block>209715200</block></capacity>
          </ScsiLun>
          <ScsiLun xsi:type="ScsiLun">
            <uuid>mpx.vmhba64:C0:T0:L0</uuid>
            <canonicalName>mpx.vmhba64:C0:T0:L0</canonicalName>
            <deviceType>cdrom</deviceType>
            <model>CD-ROM  </model>
            <vendor>NECVMWar</vendor>
          </ScsiLun>
        </val>
      </propSet>
      <propSet>
        <name>configManager.bootDeviceSystem</name>
        <val xsi:type="ManagedObjectReference" type="HostBootDeviceSystem">boot-devsys-ha</val>
      </propSet>
    </objects>
  </returnval></RetrievePropertiesExResponse>"""
)


# ---------------------------------------------------------------------------
# Codec acceptance spec -- the 12 named per-shape tests (rules 1-7)
# ---------------------------------------------------------------------------


def test_codec_rule1_entry_body_response_returnval() -> None:
    """Rule 1: Body -> {method}Response -> returnval is the parse entry point."""
    xml = _envelope(
        '<QueryBootDevicesResponse xmlns="urn:vim25">'
        "<returnval><currentBootDeviceKey>key-abc</currentBootDeviceKey></returnval>"
        "</QueryBootDevicesResponse>"
    )
    assert parse_boot_devices(xml) == {"currentBootDeviceKey": "key-abc"}


def test_codec_rule2_moref_returnval_datastore() -> None:
    """Rule 2: a returnval with a ``type`` attr + no children -> Datastore MoRef."""
    xml = _envelope(
        '<CreateNasDatastoreResponse xmlns="urn:vim25">'
        '<returnval type="Datastore">datastore-42</returnval>'
        "</CreateNasDatastoreResponse>"
    )
    assert parse_moref_result(xml, "CreateNasDatastore") == {
        "type": "Datastore",
        "value": "datastore-42",
    }


def test_codec_rule2_moref_returnval_task() -> None:
    """Rule 2: a ``*_Task`` returnval -> Task MoRef (the poll handle)."""
    xml = _envelope(
        '<MarkAsSsd_TaskResponse xmlns="urn:vim25">'
        '<returnval type="Task">haTask-ha-host-vim.host.StorageSystem.markAsSsd-9</returnval>'
        "</MarkAsSsd_TaskResponse>"
    )
    assert parse_moref_result(xml, "MarkAsSsd_Task") == {
        "type": "Task",
        "value": "haTask-ha-host-vim.host.StorageSystem.markAsSsd-9",
    }


def test_codec_rule2_moref_obj_hostsystem() -> None:
    """Rule 2: an ``obj`` element -> HostSystem MoRef, unchanged by unwrap_vim_value."""
    obj = _val_of('<obj type="HostSystem">ha-host</obj>')
    assert obj == {"type": "HostSystem", "value": "ha-host"}
    # A MoRef dict has no ``_value`` box and a non-ArrayOf ``_typeName``, so
    # the downstream unwrapper leaves it intact for ``_moref_value``.
    assert unwrap_vim_value(obj) == {"type": "HostSystem", "value": "ha-host"}
    assert _moref_value(obj) == "ha-host"


def test_codec_rule3_xsi_type_preserved_as_typename() -> None:
    """Rule 3: an ``xsi:type`` on a DataObject is preserved as ``_typeName``."""
    disk = _val_of(
        '<val xsi:type="HostScsiDisk" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<uuid>abc</uuid></val>"
    )
    assert disk["_typeName"] == "HostScsiDisk"
    assert disk["uuid"] == "abc"


def test_codec_rule4_force_list_objects_singular() -> None:
    """Rule 4: a single ``objects`` (ObjectContent) is still a list."""
    result = parse_retrieve_result(_SCSI_LUN_RETRIEVE_XML)
    assert isinstance(result["objects"], list)
    assert len(result["objects"]) == 1


def test_codec_rule4_force_list_propset_singular() -> None:
    """Rule 4: a single ``propSet`` is still a list (consumers iterate it)."""
    xml = _envelope(
        '<RetrievePropertiesExResponse xmlns="urn:vim25"><returnval><objects>'
        '<obj type="HostSystem">ha-host</obj>'
        "<propSet><name>runtime.connectionState</name><val>connected</val></propSet>"
        "</objects></returnval></RetrievePropertiesExResponse>"
    )
    result = parse_retrieve_result(xml)
    assert isinstance(result["objects"][0]["propSet"], list)
    assert len(result["objects"][0]["propSet"]) == 1


def test_codec_rule5_arrayof_single_element_collapse() -> None:
    """Rule 5: a single-element ArrayOf* stays a list (no scalar collapse)."""
    box = _val_of(
        '<val xsi:type="ArrayOfScsiLun" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<ScsiLun xsi:type="HostScsiDisk"><uuid>only</uuid></ScsiLun></val>'
    )
    assert box["_typeName"] == "ArrayOfScsiLun"
    assert isinstance(box["ScsiLun"], list)
    assert len(box["ScsiLun"]) == 1
    # Backstop: unwrap_vim_value collapses the SOAP box to the bare list.
    assert unwrap_vim_value(box) == [{"_typeName": "HostScsiDisk", "uuid": "only"}]


def test_codec_rule5_arrayof_multi_element() -> None:
    """Rule 5: a multi-element ArrayOf* -> list; unwrap yields the bare list."""
    box = _val_of(
        '<val xsi:type="ArrayOfString" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<string>a</string><string>b</string></val>"
    )
    assert box == {"_typeName": "ArrayOfString", "string": ["a", "b"]}
    assert unwrap_vim_value(box) == ["a", "b"]


def test_codec_rule6_cardinality_repeated_siblings_list() -> None:
    """Rule 6: repeated non-forced sibling tags -> list."""
    val = _val_of("<parent><item>1</item><item>2</item><item>3</item></parent>")
    assert val == {"item": [1, 2, 3]}


def test_codec_rule6_cardinality_single_scalar() -> None:
    """Rule 6: a single non-forced tag -> scalar/dict, not a list."""
    val = _val_of("<parent><only>solo</only></parent>")
    assert val == {"only": "solo"}


def test_codec_rule7_propset_name_val_shape() -> None:
    """Rule 7: a propSet -> {name, val} with the val's xsi:type preserved."""
    result = parse_retrieve_result(_SCSI_LUN_RETRIEVE_XML)
    prop = result["objects"][0]["propSet"][0]
    assert prop["name"] == "config.storageDevice.scsiLun"
    assert prop["val"]["_typeName"] == "ArrayOfScsiLun"


# ---------------------------------------------------------------------------
# Native primitive typing (codec rule 8) + downstream parity
# ---------------------------------------------------------------------------


def test_primitive_bare_boolean_coerced_to_bool() -> None:
    """Rule 8: bare ``true`` / ``false`` (no xsi:type) -> Python bool."""
    assert _val_of("<ssd>true</ssd>") is True
    assert _val_of("<local>false</local>") is False


def test_primitive_bare_int_coerced_to_int() -> None:
    """Rule 8: a bare strict integer literal -> int; zero-padded ids stay str."""
    assert _val_of("<block>209715200</block>") == 209715200
    assert isinstance(_val_of("<block>209715200</block>"), int)
    assert _val_of("<lun>0</lun>") == 0
    assert _val_of("<key>007</key>") == "007"  # zero-padded -> not coerced


def test_primitive_xsi_typed_boolean_and_int() -> None:
    """Rule 8: an explicit xsi:type drives coercion (incl. ``1``/``0`` booleans)."""
    assert _coerce_leaf("1", "xsd:boolean") is True
    assert _coerce_leaf("0", "xsd:boolean") is False
    assert _coerce_leaf("512", "xsd:int") == 512
    assert _coerce_leaf("42", "xsd:long") == 42


def test_primitive_string_with_trailing_padding_preserved() -> None:
    """A non-numeric leaf keeps its (padded) text for the consumer to strip."""
    assert _val_of("<model>Virtual disk   </model>") == "Virtual disk   "
    assert _val_of("<version>9.1.0</version>") == "9.1.0"


def test_scsi_lun_ssd_is_true_through_map_scsi_lun() -> None:
    """The crux: bare ``<ssd>true</ssd>`` survives to ``ssd is True`` end-to-end.

    This is the #3332 corruption class -- a schema-less walker would leave
    ``ssd`` as the string ``"true"`` and ``_bool_or_none`` would null it.
    """
    props = _extract_host_props(parse_retrieve_result(_SCSI_LUN_RETRIEVE_XML))
    luns = props["config.storageDevice.scsiLun"]
    assert isinstance(luns, list)
    disk = _map_scsi_lun(luns[0], None)
    assert disk["ssd"] is True
    assert disk["local"] is True
    assert disk["capacity_bytes"] == 512 * 209715200
    assert disk["device_type"] == "disk"
    assert disk["canonical_name"] == "naa.6000c290"
    assert disk["model"] == "Virtual disk"  # trailing padding stripped by the consumer
    assert disk["vendor"] == "VMware"


def test_scsi_lun_array_flows_through_extract_host_props() -> None:
    """The whole scsiLun array + bootDeviceSystem MoRef flow to the consumers."""
    props = _extract_host_props(parse_retrieve_result(_SCSI_LUN_RETRIEVE_XML))
    luns = props["config.storageDevice.scsiLun"]
    assert [lun["deviceType"] for lun in luns] == ["disk", "cdrom"]
    # The non-disk LUN nulls the HostScsiDisk-only flags through the mapper.
    cdrom = _map_scsi_lun(luns[1], None)
    assert cdrom["ssd"] is None
    assert cdrom["local"] is None
    assert cdrom["capacity_bytes"] is None
    # The bootDeviceSystem MoRef property resolves to its moid.
    assert _moref_value(props["configManager.bootDeviceSystem"]) == "boot-devsys-ha"


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def test_build_service_content_envelope_wellformed() -> None:
    """RetrieveServiceContent: well-formed, _this on the ServiceInstance singleton."""
    root = fromstring(build_service_content_envelope())
    method = root[0][0]
    assert _local(method.tag) == "RetrieveServiceContent"
    this = method[0]
    assert _local(this.tag) == "_this"
    assert this.get("type") == "ServiceInstance"
    assert this.text == "ServiceInstance"


def test_build_login_envelope_this_and_credentials() -> None:
    """Login: _this type/moId is the SessionManager; userName/password present."""
    root = fromstring(
        build_login_envelope("ha-sessionmgr", username="root", password="s3cr3t", locale="en")
    )
    method = root[0][0]
    assert _local(method.tag) == "Login"
    children = {_local(c.tag): c for c in method}
    assert children["_this"].get("type") == "SessionManager"
    assert children["_this"].text == "ha-sessionmgr"
    assert children["userName"].text == "root"
    assert children["password"].text == "s3cr3t"
    assert children["locale"].text == "en"


def test_build_login_envelope_escapes_special_chars() -> None:
    """Login: all five XML entities in the password are escaped on the wire."""
    envelope = build_login_envelope("ha-sessionmgr", username="root", password="a&b<c>d\"e'f")
    assert "&amp;" in envelope
    assert "&lt;" in envelope
    assert "&gt;" in envelope
    assert "&quot;" in envelope
    assert "&apos;" in envelope
    # The raw special-char password does not appear unescaped in the wire text.
    assert "a&b<c>d" not in envelope
    # It round-trips: the parsed <password> text is the original secret.
    root = fromstring(envelope)
    password = next(c for c in root[0][0] if _local(c.tag) == "password")
    assert password.text == "a&b<c>d\"e'f"


def test_build_logout_envelope() -> None:
    """Logout: well-formed, _this on the SessionManager moid."""
    root = fromstring(build_logout_envelope("ha-sessionmgr"))
    method = root[0][0]
    assert _local(method.tag) == "Logout"
    this = method[0]
    assert this.get("type") == "SessionManager"
    assert this.text == "ha-sessionmgr"


def test_build_retrieve_properties_ex_envelope_from_vijson_body() -> None:
    """RetrievePropertiesEx: the VI-JSON specSet serialises to SOAP faithfully."""
    body = retrieve_properties_body(
        "HostSystem",
        ["ha-host"],
        ["config.storageDevice.scsiLun", "configManager.bootDeviceSystem"],
    )
    root = fromstring(
        build_retrieve_properties_ex_envelope(
            "ha-property-collector", body["specSet"], body["options"]
        )
    )
    method = root[0][0]
    assert _local(method.tag) == "RetrievePropertiesEx"
    this = next(c for c in method if _local(c.tag) == "_this")
    assert this.get("type") == "PropertyCollector"
    assert this.text == "ha-property-collector"
    spec = next(c for c in method if _local(c.tag) == "specSet")
    prop_spec = next(c for c in spec if _local(c.tag) == "propSet")
    assert next(c for c in prop_spec if _local(c.tag) == "type").text == "HostSystem"
    path_sets = [c.text for c in prop_spec if _local(c.tag) == "pathSet"]
    assert path_sets == ["config.storageDevice.scsiLun", "configManager.bootDeviceSystem"]
    obj = next(c for c in spec if _local(c.tag) == "objectSet")[0]
    assert obj.get("type") == "HostSystem"
    assert obj.text == "ha-host"


def test_build_query_boot_devices_envelope() -> None:
    """QueryBootDevices: well-formed, _this on the HostBootDeviceSystem moid."""
    root = fromstring(build_query_boot_devices_envelope("bootDeviceSystem-ha-host"))
    method = root[0][0]
    assert _local(method.tag) == "QueryBootDevices"
    this = method[0]
    assert this.get("type") == "HostBootDeviceSystem"
    assert this.text == "bootDeviceSystem-ha-host"


def test_build_create_nas_datastore_envelope_fields_ordered() -> None:
    """CreateNasDatastore: HostNasVolumeSpec fields in WSDL sequence order."""
    spec = {
        "_typeName": "HostNasVolumeSpec",
        "remoteHost": "nfs.example",
        "remotePath": "/export/vol",
        "localPath": "nfs-ds",
        "accessMode": "readWrite",
        "type": "NFS",
    }
    root = fromstring(build_create_nas_datastore_envelope("dsSystem-ha", spec))
    method = root[0][0]
    assert _local(method.tag) == "CreateNasDatastore"
    this = next(c for c in method if _local(c.tag) == "_this")
    assert this.get("type") == "HostDatastoreSystem"
    spec_el = next(c for c in method if _local(c.tag) == "spec")
    field_names = [_local(c.tag) for c in spec_el]
    assert field_names == ["remoteHost", "remotePath", "localPath", "accessMode", "type"]
    values = {_local(c.tag): c.text for c in spec_el}
    assert values["remoteHost"] == "nfs.example"
    assert values["localPath"] == "nfs-ds"
    # The VI-JSON _typeName discriminator is dropped -- SOAP types by position.
    assert "_typeName" not in field_names


def test_build_create_nas_datastore_envelope_skips_absent_fields() -> None:
    """CreateNasDatastore: absent optional fields are not emitted."""
    spec = {
        "remoteHost": "nfs.example",
        "remotePath": "/export/vol",
        "localPath": "nfs-ds",
        "accessMode": "readOnly",
    }
    root = fromstring(build_create_nas_datastore_envelope("dsSystem-ha", spec))
    spec_el = next(c for c in root[0][0] if _local(c.tag) == "spec")
    field_names = [_local(c.tag) for c in spec_el]
    assert field_names == ["remoteHost", "remotePath", "localPath", "accessMode"]
    assert "type" not in field_names


def test_build_mark_ssd_envelope_toggles_method() -> None:
    """MarkAs*: ssd=True -> MarkAsSsd_Task, ssd=False -> MarkAsNonSsd_Task."""
    ssd_root = fromstring(build_mark_ssd_envelope("storageSystem-ha", "0200000000600a", ssd=True))
    non_ssd_root = fromstring(
        build_mark_ssd_envelope("storageSystem-ha", "0200000000600a", ssd=False)
    )
    ssd_method = ssd_root[0][0]
    non_ssd_method = non_ssd_root[0][0]
    assert _local(ssd_method.tag) == "MarkAsSsd_Task"
    assert _local(non_ssd_method.tag) == "MarkAsNonSsd_Task"
    this = next(c for c in ssd_method if _local(c.tag) == "_this")
    assert this.get("type") == "HostStorageSystem"
    disk = next(c for c in ssd_method if _local(c.tag) == "scsiDiskUuid")
    assert disk.text == "0200000000600a"


def test_build_mark_ssd_envelope_escapes_disk_uuid() -> None:
    """MarkAs*: the scsiDiskUuid is XML-escaped (defence-in-depth)."""
    envelope = build_mark_ssd_envelope("storageSystem-ha", "uuid&<danger>", ssd=True)
    assert "&amp;" in envelope
    assert "uuid&<danger>" not in envelope
    disk = next(c for c in fromstring(envelope)[0][0] if _local(c.tag) == "scsiDiskUuid")
    assert disk.text == "uuid&<danger>"


# ---------------------------------------------------------------------------
# Service content + boot devices parse (downstream connector consumers)
# ---------------------------------------------------------------------------


def test_parse_service_content_morefs_and_about() -> None:
    """RetrieveServiceContent -> propertyCollector/sessionManager MoRefs + about."""
    xml = _envelope(
        '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
        '<propertyCollector type="PropertyCollector">ha-property-collector</propertyCollector>'
        '<sessionManager type="SessionManager">ha-sessionmgr</sessionManager>'
        "<about><version>9.1.0</version><apiType>HostAgent</apiType>"
        "<fullName>VMware ESXi 9.1.0</fullName></about>"
        "</returnval></RetrieveServiceContentResponse>"
    )
    sc = parse_service_content(xml)
    assert sc["propertyCollector"] == {
        "type": "PropertyCollector",
        "value": "ha-property-collector",
    }
    assert sc["sessionManager"] == {"type": "SessionManager", "value": "ha-sessionmgr"}
    assert sc["about"]["version"] == "9.1.0"
    assert sc["about"]["apiType"] == "HostAgent"


def test_parse_boot_devices_current_key() -> None:
    """QueryBootDevices -> HostBootDeviceInfo dict carrying currentBootDeviceKey."""
    xml = _envelope(
        '<QueryBootDevicesResponse xmlns="urn:vim25"><returnval>'
        "<currentBootDeviceKey>key-vim.host.BootDevice-1</currentBootDeviceKey>"
        "</returnval></QueryBootDevicesResponse>"
    )
    info = parse_boot_devices(xml)
    assert info["currentBootDeviceKey"] == "key-vim.host.BootDevice-1"


def test_parse_returnval_absent_yields_empty() -> None:
    """A response with no returnval degrades to an empty dict / None, not a crash."""
    xml = _envelope('<RetrievePropertiesExResponse xmlns="urn:vim25"/>')
    assert parse_retrieve_result(xml) == {}
    xml_moref = _envelope('<CreateNasDatastoreResponse xmlns="urn:vim25"/>')
    assert parse_moref_result(xml_moref, "CreateNasDatastore") is None


# ---------------------------------------------------------------------------
# Fault parsing + mapping discriminators
# ---------------------------------------------------------------------------


def _fault_envelope(detail_child: str, faultstring: str = "boom") -> str:
    return _envelope(
        "<soapenv:Fault>"
        "<faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{faultstring}</faultstring>"
        f"<detail>{detail_child}</detail>"
        "</soapenv:Fault>"
    )


def test_parse_soap_fault_invalid_login_discriminator() -> None:
    """InvalidLogin -> fault_type the connector routes to ConnectorAuthError."""
    xml = _fault_envelope(
        '<InvalidLogin xmlns="urn:vim25" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="InvalidLogin"/>',
        faultstring="Cannot complete login due to an incorrect user name or password.",
    )
    fault = parse_soap_fault(xml)
    assert isinstance(fault, SoapFault)
    assert fault.fault_type == "InvalidLogin"
    assert fault.faultcode == "ServerFaultCode"
    # Connector mapping: {InvalidLogin, NoPermission} -> ConnectorAuthError.
    assert fault.fault_type in {"InvalidLogin", "NoPermission"}


def test_parse_soap_fault_platform_config_fault_discriminator() -> None:
    """PlatformConfigFault -> fault_type the connector routes to RuntimeError."""
    xml = _fault_envelope(
        '<PlatformConfigFault xmlns="urn:vim25" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="PlatformConfigFault">'
        "<text>NFS mount failed</text></PlatformConfigFault>"
    )
    fault = parse_soap_fault(xml)
    assert isinstance(fault, SoapFault)
    assert fault.fault_type == "PlatformConfigFault"


def test_parse_soap_fault_no_permission_by_localname() -> None:
    """A detail child with no xsi:type falls back to its element local-name."""
    xml = _fault_envelope(
        '<NoPermission xmlns="urn:vim25"><privilegeId>System.View</privilegeId></NoPermission>'
    )
    fault = parse_soap_fault(xml)
    assert isinstance(fault, SoapFault)
    assert fault.fault_type == "NoPermission"


def test_parse_soap_fault_returns_none_for_non_fault() -> None:
    """A well-formed non-fault body -> None (the caller then proceeds)."""
    xml = _envelope(
        '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
        "<about><version>9.1.0</version></about></returnval></RetrieveServiceContentResponse>"
    )
    assert parse_soap_fault(xml) is None


def test_parse_soap_fault_returns_none_for_unparseable_body() -> None:
    """A body that is not well-formed XML -> None, never an exception."""
    assert parse_soap_fault("not xml at all <<<") is None
    assert parse_soap_fault("") is None


# ---------------------------------------------------------------------------
# Credential posture
# ---------------------------------------------------------------------------


def test_login_credential_never_in_fault_repr_or_parse() -> None:
    """No soap.py artefact around a Login echoes the password.

    The Login envelope string is the *only* carrier of the credential (and
    escaped there); the fault parsed from a rejected Login, and its repr,
    carry only the vendor faultstring -- never the password. (The connector
    additionally keeps the envelope out of logs and the flight-recorder
    span; that surface is asserted in the connector-wiring tests.)
    """
    secret = "sup3r-s3cr3t&pw"  # a test literal, not a real credential
    envelope = build_login_envelope("ha-sessionmgr", username="root", password=secret)
    # The escaped credential is present only inside the <password> element.
    assert "sup3r-s3cr3t&amp;pw" in envelope
    assert secret not in envelope  # the raw (unescaped) form never appears

    invalid_login = _fault_envelope(
        '<InvalidLogin xmlns="urn:vim25"/>',
        faultstring="Cannot complete login due to an incorrect user name or password.",
    )
    fault = parse_soap_fault(invalid_login)
    assert fault is not None
    assert secret not in repr(fault)
    assert "sup3r-s3cr3t" not in repr(fault)
    assert secret not in (fault.faultstring or "")
