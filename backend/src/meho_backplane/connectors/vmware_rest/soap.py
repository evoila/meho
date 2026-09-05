# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-rolled SOAP 1.1 codec for the standalone-ESXi vmomi surface (#3363).

A standalone ESXi host does **not** serve the VI-JSON surface
(``/sdk/vim25/{release}/…``) -- that is vCenter-only; every VI-JSON POST
there returns HTTP 500 with a SOAP expat fault because ``/sdk``
XML-parses the body. ESXi serves vmomi only as **SOAP 1.1 over
``POST /sdk``**. This module is the transport-agnostic half of the ESXi
branch inside :class:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector`:
it **builds** the request envelopes and **parses** the response
envelopes back into the *exact* VI-JSON dict shapes the unchanged
downstream consumers already read (``vim_body.unwrap_vim_value``,
``typed_ops_host_storage_devices._extract_host_props`` /
``_map_scsi_lun``, ``vim_task`` poll, the host composites), so nothing
below the connector's two chokepoints has to know a SOAP wire exists.

Parsing uses ``defusedxml.ElementTree.fromstring`` (the sanctioned
XXE-hardened parser already in the tree; annotating the parsed nodes
with the stdlib ``Element`` type would re-introduce the ``xml.etree``
import Semgrep flags, so nodes surface as ``Any`` -- the same posture
:mod:`meho_backplane.connectors.pfsense.ops_read` takes). There is no
sanctioned XML *serialiser* in the repo and ``xml.etree`` is
Semgrep-flagged, so request envelopes are built as **hand-rolled
per-method string templates** with a tiny local :func:`_xml_escape`
covering the five XML entities.

Codec acceptance spec (the deserialiser is the single point where parity
with the VI-JSON path can silently break -- the mock-vs-hardware trap
that let #3332 ship green). :func:`_soap_val_to_json` satisfies, rule by
rule:

1. **Entry:** ``soapenv:Body`` -> ``{method}Response`` -> its
   ``returnval`` child (if present) -- :func:`_parse_returnval`.
2. **MoRef:** an element with a ``type`` attribute and no child elements
   -> ``{"type": <attr>, "value": <text>}`` (``ManagedObjectReference``).
   Covers ``CreateNasDatastore`` -> Datastore, ``*_Task`` -> Task,
   ``obj`` -> HostSystem, and MoRef-valued properties
   (``configManager.bootDeviceSystem``).
3. **``xsi:type`` -> ``_typeName``:** the DataObject discriminator is
   preserved on every complex element that carries an ``xsi:type`` (the
   key consumers guard on -- ``VirtualDisk``, ``ArrayOf*`` containers).
4. **Force-list local-names** ``{objects, propSet}`` -> always a
   ``list``, even when singular (consumers iterate unconditionally).
5. **``ArrayOf*`` containers** -> children always a ``list`` ->
   ``{"_typeName": "ArrayOfX", "X": [...]}``, **including the
   single-element collapse**. Backstop: ``unwrap_vim_value`` already
   tolerates exactly this SOAP-flavoured box (see its docstring), which
   shrinks the single-point-of-failure risk.
6. **Cardinality:** repeated sibling tags -> ``list``; a single
   non-forced tag -> scalar/dict.
7. **``propSet``** element -> ``{"name": <name text>, "val": <converted
   val, xsi:type preserved>}`` (the generic complex walk yields this).
8. **Native primitive typing (the schema-less trap).** A schema-less
   walker emits leaf text as strings; the real ``scsiLun`` envelope
   carries **bare** ``<ssd>true</ssd>`` / ``<localDisk>true</localDisk>``
   with **no** ``xsi:type``, and the downstream ``_bool_or_none("true")``
   returns ``None`` -- silently dropping the flags (the #3332 corruption
   class). :func:`_coerce_leaf` honours an **explicit** ``xsi:type``
   (``xsd:boolean`` -> ``bool``, integer -> ``int``, float -> ``float``,
   any other annotated leaf -> its raw text), and for a **bare** leaf
   coerces only ``"true"`` / ``"false"`` to ``bool``. A bare numeric leaf
   is left as text: vim25 carries string-typed keys whose values read as
   integers (``currentBootDeviceKey == "8"``), and coercing those to
   ``int`` by value broke the ``isinstance(..., str)`` consumer on real
   hardware (#3363 State-2); the genuinely-integer consumers
   (``capacity`` ``block`` / ``blockSize``) normalise a numeric string via
   ``_int_or_none`` already, so ``capacity_bytes`` is ``int`` either way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY

__all__ = [
    "SOAP_CONTENT_TYPE",
    "SoapFault",
    "build_create_nas_datastore_envelope",
    "build_login_envelope",
    "build_logout_envelope",
    "build_mark_ssd_envelope",
    "build_query_boot_devices_envelope",
    "build_retrieve_properties_ex_envelope",
    "build_service_content_envelope",
    "parse_boot_devices",
    "parse_moref_result",
    "parse_retrieve_result",
    "parse_service_content",
    "parse_soap_fault",
    "soap_action_for_version",
]

# --- Wire constants --------------------------------------------------------

#: SOAP 1.1 envelope namespace.
_SOAP_ENV_NS: Final = "http://schemas.xmlsoap.org/soap/envelope/"
#: The vmomi method namespace (default ns on every method element).
_VIM_NS: Final = "urn:vim25"
#: XML-Schema-instance namespace (the ``xsi:type`` attribute lives here).
_XSI_NS: Final = "http://www.w3.org/2001/XMLSchema-instance"
#: XML-Schema namespace (declared so ``xsd:*`` xsi:type values resolve).
_XSD_NS: Final = "http://www.w3.org/2001/XMLSchema"
#: Clark-notation key of the ``xsi:type`` attribute on a parsed node.
_XSI_TYPE_ATTR: Final = f"{{{_XSI_NS}}}type"

#: ``Content-Type`` every ``/sdk`` SOAP POST carries.
SOAP_CONTENT_TYPE: Final = "text/xml; charset=utf-8"


def soap_action_for_version(api_version: str) -> str:
    """The ``SOAPAction`` header value that pins a ``/sdk`` POST to a vim API version.

    An **empty** ``SOAPAction`` resolves the method against the host's
    baseline schema -- live-observed on standalone ESXi 9.1 as ``vim25/2.5u2``,
    which predates ``RetrievePropertiesEx`` (4.1), ``MarkAs*_Task`` (5.x), and
    the datastore-mount write, so those posts fault ``InvalidRequest: Unable to
    resolve WSDL method name`` (#3363 State-2). Announcing the host's
    ``ServiceContent.about.apiVersion`` (``9.1.0.0``) binds the post to that
    schema instead. Only ``RetrieveServiceContent`` + ``SessionManager.Login``
    ride the baseline (they exist there, and the version is unknown until the
    former returns it); every op past the bootstrap carries this.
    """
    return f"{_VIM_NS}/{api_version}"


#: The ``ServiceInstance`` singleton MoRef -- the fixed bootstrap object
#: ``RetrieveServiceContent`` is invoked on (type == moId == the literal).
SERVICE_INSTANCE_MOID: Final = "ServiceInstance"

#: ``_typeName`` prefix of the boxed-array components (mirrors
#: :data:`vim_body._ARRAY_OF_PREFIX`; consumers key on it).
_ARRAY_OF_PREFIX: Final = "ArrayOf"

#: Local-names whose children are *always* a list even when singular
#: (codec rule 4) -- the RetrieveResult iteration seams.
_FORCE_LIST_LOCALNAMES: Final = frozenset({"objects", "propSet"})

#: ``xsd`` primitive local-names coerced to :class:`int` (codec rule 8).
_INT_XSD_TYPES: Final = frozenset(
    {
        "int",
        "integer",
        "long",
        "short",
        "byte",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
        "unsignedLong",
        "nonNegativeInteger",
        "positiveInteger",
    }
)
#: ``xsd`` primitive local-names coerced to :class:`float` (codec rule 8).
_FLOAT_XSD_TYPES: Final = frozenset({"double", "float", "decimal"})
#: Canonical child order of a ``HostNasVolumeSpec`` (vim25 WSDL sequence);
#: only present, non-``None`` fields are serialised (#3363 datastore mount).
_HOST_NAS_VOLUME_SPEC_FIELDS: Final = (
    "remoteHost",
    "remotePath",
    "localPath",
    "accessMode",
    "type",
    "userName",
    "password",
    "securityType",
)


# --- Fault -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SoapFault:
    """A parsed ``<soapenv:Fault>`` -- the connector maps it to an error.

    ``fault_type`` is the discriminator localName of the vim ``detail``
    child (``InvalidLogin``, ``NoPermission``, ``PlatformConfigFault``,
    ``HostConfigFault``, ``InvalidArgument`` ...), taken from the child's
    ``xsi:type`` when present and otherwise from its element local-name.
    The connector's ``_post_soap`` routes ``InvalidLogin`` / ``NoPermission``
    to ``ConnectorAuthError`` and the write faults to ``RuntimeError``.

    Carries only ``faultcode`` / ``faultstring`` / ``fault_type`` -- none
    of which echo the Login envelope, so the default ``repr`` cannot leak
    a credential.
    """

    faultcode: str | None
    faultstring: str | None
    fault_type: str | None


# --- Small XML helpers -----------------------------------------------------


def _xml_escape(value: str) -> str:
    """Escape the five XML entities (``&`` first, so it is not doubled)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _local(tag: Any) -> str:
    """Local-name of a Clark-notation tag/attribute (``{ns}foo`` -> ``foo``)."""
    text = tag if isinstance(tag, str) else str(tag)
    return text.rpartition("}")[2]


def _strip_qname_prefix(qname: str) -> str:
    """Local part of an ``xsi:type`` value (``xsd:boolean`` -> ``boolean``)."""
    return qname.rpartition(":")[2]


def _this(mo_type: str, moid: str) -> str:
    """The ``<_this type="…">moId</_this>`` self-reference every body carries."""
    return f'<_this type="{_xml_escape(mo_type)}">{_xml_escape(moid)}</_this>'


def _envelope(method: str, inner: str) -> str:
    """Wrap a method element (``inner`` is its full ``<method>…</method>``)."""
    return (
        "<soapenv:Envelope "
        f'xmlns:soapenv="{_SOAP_ENV_NS}" '
        f'xmlns:xsi="{_XSI_NS}" '
        f'xmlns:xsd="{_XSD_NS}">'
        "<soapenv:Body>"
        f"{inner}"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def _method(name: str, body: str) -> str:
    """A ``urn:vim25`` method element (default ns so children inherit it)."""
    return f'<{name} xmlns="{_VIM_NS}">{body}</{name}>'


# --- Envelope builders -----------------------------------------------------


def build_service_content_envelope() -> str:
    """``ServiceInstance.RetrieveServiceContent`` -- the unauthenticated bootstrap read.

    Returns the ServiceContent (propertyCollector + sessionManager MoRefs
    and ``about``); on ESXi ``about.apiType == "HostAgent"``.
    """
    return _envelope(
        "RetrieveServiceContent",
        _method(
            "RetrieveServiceContent",
            _this("ServiceInstance", SERVICE_INSTANCE_MOID),
        ),
    )


def build_login_envelope(
    session_manager_moid: str,
    *,
    username: str,
    password: str,
    locale: str | None = None,
) -> str:
    """``SessionManager.Login`` -- the credential is XML-escaped in ``<password>``.

    The returned string is the *only* place the password appears; the
    connector never passes this envelope to a log call or serialises it
    into the flight-recorder vendor-call span (#3363 credential posture).
    """
    parts = [
        _this("SessionManager", session_manager_moid),
        f"<userName>{_xml_escape(username)}</userName>",
        f"<password>{_xml_escape(password)}</password>",
    ]
    if locale is not None:
        parts.append(f"<locale>{_xml_escape(locale)}</locale>")
    return _envelope("Login", _method("Login", "".join(parts)))


def build_logout_envelope(session_manager_moid: str) -> str:
    """``SessionManager.Logout`` -- best-effort teardown on the pooled client."""
    return _envelope(
        "Logout",
        _method("Logout", _this("SessionManager", session_manager_moid)),
    )


def build_retrieve_properties_ex_envelope(
    property_collector_moid: str,
    spec_set: Sequence[Mapping[str, Any]],
    options: Mapping[str, Any] | None = None,
) -> str:
    """``PropertyCollector.RetrievePropertiesEx`` from the VI-JSON body shape.

    *spec_set* is the ``specSet`` list produced by
    ``vim_body.retrieve_properties_body`` (one ``PropertyFilterSpec`` per
    filter: ``propSet`` PropertySpecs with ``type`` + ``pathSet``,
    ``objectSet`` ObjectSpecs each wrapping an ``obj`` MoRef). The
    ``_typeName`` request discriminators are dropped -- SOAP announces
    types by element position, not by tag.
    """
    filters: list[str] = []
    for spec in spec_set:
        prop_specs: list[str] = []
        for prop_spec in spec.get("propSet", []) or []:
            paths = "".join(
                f"<pathSet>{_xml_escape(str(path))}</pathSet>"
                for path in prop_spec.get("pathSet", []) or []
            )
            all_props = "<all>true</all>" if prop_spec.get("all") else ""
            mo_type = _xml_escape(str(prop_spec.get("type", "")))
            prop_specs.append(f"<propSet><type>{mo_type}</type>{all_props}{paths}</propSet>")
        object_specs: list[str] = []
        for object_spec in spec.get("objectSet", []) or []:
            obj = object_spec.get("obj", {}) or {}
            obj_type = _xml_escape(str(obj.get("type", "")))
            obj_moid = _xml_escape(str(obj.get("value", "")))
            object_specs.append(f'<objectSet><obj type="{obj_type}">{obj_moid}</obj></objectSet>')
        filters.append(f"<specSet>{''.join(prop_specs)}{''.join(object_specs)}</specSet>")
    max_objects = None if options is None else options.get("maxObjects")
    options_xml = (
        f"<options><maxObjects>{int(max_objects)}</maxObjects></options>"
        if max_objects is not None
        else "<options></options>"
    )
    body = _this("PropertyCollector", property_collector_moid) + "".join(filters) + options_xml
    return _envelope("RetrievePropertiesEx", _method("RetrievePropertiesEx", body))


def build_query_boot_devices_envelope(boot_device_system_moid: str) -> str:
    """``HostBootDeviceSystem.QueryBootDevices`` -- best-effort boot-device read."""
    return _envelope(
        "QueryBootDevices",
        _method(
            "QueryBootDevices",
            _this("HostBootDeviceSystem", boot_device_system_moid),
        ),
    )


def build_create_nas_datastore_envelope(
    datastore_system_moid: str,
    spec: Mapping[str, Any],
) -> str:
    """``HostDatastoreSystem.CreateNasDatastore`` (synchronous; returns a Datastore MoRef).

    *spec* is the ``HostNasVolumeSpec`` dict the composite builds
    (``remoteHost`` / ``remotePath`` / ``localPath`` / ``accessMode`` /
    ``type`` ...); fields are emitted in the vim25 WSDL sequence order,
    skipping absent / ``None`` ones. The ``_typeName`` request
    discriminator is dropped.
    """
    fields = "".join(
        f"<{field}>{_xml_escape(str(spec[field]))}</{field}>"
        for field in _HOST_NAS_VOLUME_SPEC_FIELDS
        if spec.get(field) is not None
    )
    body = _this("HostDatastoreSystem", datastore_system_moid) + f"<spec>{fields}</spec>"
    return _envelope("CreateNasDatastore", _method("CreateNasDatastore", body))


def build_mark_ssd_envelope(
    storage_system_moid: str,
    scsi_disk_uuid: str,
    *,
    ssd: bool,
) -> str:
    """``HostStorageSystem.MarkAsSsd_Task`` (``ssd``) / ``MarkAsNonSsd_Task`` (else).

    Both return a ``*_Task`` MoRef the caller polls via ``Task.info``.
    """
    method = "MarkAsSsd_Task" if ssd else "MarkAsNonSsd_Task"
    body = (
        _this("HostStorageSystem", storage_system_moid)
        + f"<scsiDiskUuid>{_xml_escape(scsi_disk_uuid)}</scsiDiskUuid>"
    )
    return _envelope(method, _method(method, body))


# --- Deserialiser core -----------------------------------------------------


def _coerce_leaf(text: str | None, xsi_type_raw: str | None) -> bool | int | float | str:
    """Codec rule 8: coerce a leaf's text to a native primitive.

    An **explicit** ``xsi:type`` is authoritative: ``xsd:boolean`` ->
    ``bool``, an integer type -> ``int``, a float type -> ``float``, and
    every other annotated leaf (``xsd:string`` included) is returned as its
    raw text -- so a string whose value happens to read as a number or as
    ``"true"`` is never re-typed against its own annotation.

    A **bare** leaf (no ``xsi:type`` -- the ESXi norm for primitives, #3363
    live) coerces only ``"true"`` / ``"false"`` to ``bool``: the real
    ``scsiLun`` envelope carries bare ``<ssd>true</ssd>`` / ``<localDisk>``,
    and the downstream ``_bool_or_none`` needs a real ``bool``. A bare
    numeric leaf is deliberately **left as text** -- vim25 carries
    string-typed keys whose values read as integers
    (``HostBootDeviceInfo.currentBootDeviceKey == "8"``, the ``bootDevices``
    key), and value-based ``int`` coercion silently broke the consumer's
    ``isinstance(..., str)`` guard on real hardware; the genuinely-integer
    consumers (``HostScsiDisk.capacity`` ``block`` / ``blockSize``) already
    normalise a numeric string through ``_int_or_none``, so ``capacity_bytes``
    lands as ``int`` either way and output parity holds. The original
    (un-stripped) text is returned so trailing-padded strings (``model`` /
    ``vendor``) survive for the consumer's own ``.strip()``.
    """
    raw = text if text is not None else ""
    stripped = raw.strip()
    if xsi_type_raw is not None:
        xsi_local = _strip_qname_prefix(xsi_type_raw)
        if xsi_local == "boolean":
            return stripped in ("true", "1")
        if xsi_local in _INT_XSD_TYPES:
            try:
                return int(stripped)
            except ValueError:
                return raw
        if xsi_local in _FLOAT_XSD_TYPES:
            try:
                return float(stripped)
            except ValueError:
                return raw
        return raw
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    return raw


def _soap_val_to_json(element: Any) -> Any:
    """Recursively convert a vmomi SOAP element to its VI-JSON dict/scalar shape.

    See the module docstring for the rule-by-rule codec spec. Rule
    precedence per element: MoRef (rule 2) -> leaf primitive (rule 8) ->
    complex element (rules 3-7).
    """
    children = list(element)

    # Rule 2 -- MoRef: a ``type`` attribute and no child elements.
    if not children and element.get("type") is not None:
        return {"type": element.get("type"), "value": (element.text or "").strip()}

    xsi_type_raw = element.get(_XSI_TYPE_ATTR)
    xsi_local = _strip_qname_prefix(xsi_type_raw) if xsi_type_raw is not None else None
    is_array_container = xsi_local is not None and xsi_local.startswith(_ARRAY_OF_PREFIX)

    # Rule 5 -- an *empty* ArrayOf* container: no children, but its xsi:type
    # marks it a collection, so it stays list-shaped. Without this guard the
    # childless element falls through to the rule-8 leaf branch and yields
    # ``""`` (empty string) rather than ``[]`` -- a shape break vs the
    # force-list guarantee the non-empty case makes. The member key is the
    # ``ArrayOf`` prefix stripped (``ArrayOfScsiLun`` -> ``ScsiLun``); its
    # exact spelling is immaterial because ``unwrap_vim_value`` collapses any
    # single-list-payload ``ArrayOf*`` box to the bare list.
    if not children and is_array_container and xsi_local is not None:
        return {VIM_TYPE_NAME_KEY: xsi_local, xsi_local[len(_ARRAY_OF_PREFIX) :]: []}

    # Rule 8 -- leaf primitive.
    if not children:
        return _coerce_leaf(element.text, xsi_type_raw)

    # Complex element (rules 3-7).
    grouped: dict[str, list[Any]] = {}
    order: list[str] = []
    for child in children:
        name = _local(child.tag)
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(child)

    result: dict[str, Any] = {}
    if xsi_local is not None:
        # Rule 3 / rule 5 -- preserve the DataObject / ArrayOf discriminator.
        result[VIM_TYPE_NAME_KEY] = xsi_local
    for name in order:
        converted = [_soap_val_to_json(member) for member in grouped[name]]
        # Rule 4 (forced local-names) + rule 5 (ArrayOf) + rule 6 (cardinality).
        force_list = is_array_container or name in _FORCE_LIST_LOCALNAMES
        result[name] = converted if (force_list or len(converted) > 1) else converted[0]
    return result


def _find_child(parent: Any, localname: str) -> Any:
    """First direct child of *parent* whose local-name is *localname*, else ``None``."""
    if parent is None:
        return None
    for child in parent:
        if _local(child.tag) == localname:
            return child
    return None


def _find_body(root: Any) -> Any:
    """The ``soapenv:Body`` element (namespace-agnostic by local-name)."""
    if _local(root.tag) == "Body":
        return root
    return _find_child(root, "Body")


def _parse_returnval(xml: str, method: str) -> Any:
    """Codec rule 1: ``Body`` -> ``{method}Response`` -> ``returnval`` (converted).

    Returns ``None`` when there is no ``returnval`` (an empty result).
    """
    root = fromstring(xml)
    body = _find_body(root)
    if body is None:
        return None
    response = _find_child(body, f"{method}Response")
    if response is None:
        # Defensive: fall back to the first element child of Body.
        response = next(iter(body), None)
    if response is None:
        return None
    returnval = _find_child(response, "returnval")
    if returnval is None:
        return None
    return _soap_val_to_json(returnval)


# --- Public parsers --------------------------------------------------------


def parse_soap_fault(xml: str) -> SoapFault | None:
    """Parse a ``<soapenv:Fault>`` body; ``None`` when the body is not a fault.

    Run on **both** 200 and 500 responses *before* ``raise_for_status``: a
    vim25 fault is HTTP 500 with a ``<soapenv:Fault>`` body, but the parse
    is the authority, not the status code. A body that does not parse or
    carries no ``Fault`` returns ``None`` so the caller proceeds normally.
    """
    try:
        root = fromstring(xml)
    except ParseError:
        return None
    body = _find_body(root)
    fault = _find_child(body, "Fault")
    if fault is None:
        return None
    faultcode_el = _find_child(fault, "faultcode")
    faultstring_el = _find_child(fault, "faultstring")
    faultcode = faultcode_el.text if faultcode_el is not None else None
    faultstring = faultstring_el.text if faultstring_el is not None else None
    fault_type: str | None = None
    detail = _find_child(fault, "detail")
    if detail is not None:
        detail_child = next(iter(detail), None)
        if detail_child is not None:
            xsi_type_raw = detail_child.get(_XSI_TYPE_ATTR)
            fault_type = (
                _strip_qname_prefix(xsi_type_raw)
                if xsi_type_raw is not None
                else _local(detail_child.tag)
            )
    return SoapFault(faultcode=faultcode, faultstring=faultstring, fault_type=fault_type)


def parse_service_content(xml: str) -> dict[str, Any]:
    """Parse ``RetrieveServiceContentResponse`` -> the ServiceContent dict.

    Carries ``propertyCollector`` / ``sessionManager`` (MoRef
    ``{type, value}``) and ``about`` (``version`` / ``apiType`` / ...).
    """
    content = _parse_returnval(xml, "RetrieveServiceContent")
    return content if isinstance(content, dict) else {}


def parse_retrieve_result(xml: str) -> dict[str, Any]:
    """Parse ``RetrievePropertiesExResponse`` -> the ``RetrieveResult`` dict.

    ``{"objects": [{"obj": <MoRef>, "propSet": [{"name", "val"}, ...]}, ...]}``
    -- the exact shape ``_extract_host_props`` consumes (``objects`` and
    ``propSet`` force-listed, rule 4).
    """
    result = _parse_returnval(xml, "RetrievePropertiesEx")
    return result if isinstance(result, dict) else {}


def parse_moref_result(xml: str, method: str) -> dict[str, str] | None:
    """Parse a synchronous ``{method}Response`` whose ``returnval`` is a MoRef.

    ``CreateNasDatastore`` -> Datastore MoRef; ``MarkAs*_Task`` -> Task
    MoRef. Returns ``{"type", "value"}`` (rule 2) or ``None`` when there
    is no ``returnval``.
    """
    val = _parse_returnval(xml, method)
    if isinstance(val, dict) and "value" in val:
        return {"type": str(val.get("type", "")), "value": str(val["value"])}
    return None


def parse_boot_devices(xml: str) -> dict[str, Any]:
    """Parse ``QueryBootDevicesResponse`` -> the ``HostBootDeviceInfo`` dict.

    Carries ``currentBootDeviceKey`` (the only field the caller reads).
    """
    info = _parse_returnval(xml, "QueryBootDevices")
    return info if isinstance(info, dict) else {}
