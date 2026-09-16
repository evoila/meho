# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared vim (VI-JSON) wire-format helpers: request bodies (#3103) + response unwrap (#3106).

VI-JSON's wire format requires the ``_typeName`` discriminator on every
DataObject in a request body: the pinned ``vi-json.yaml`` derives all
data objects from ``Any``, whose ``required`` list names ``_typeName``,
and a live vCenter 8.0.3 **rejects** un-annotated bodies outright -- a
controlled differential against
``POST /sdk/vim25/8.0.3.0/PropertyCollector/propertyCollector/RetrievePropertiesEx``
returned ``500 InvalidArgument`` (vim fault message ``Invalid MoRef
field: pathSet``) for the bare body and ``200 RetrieveResult`` for the
same body with ``PropertyFilterSpec`` / ``PropertySpec`` / ``ObjectSpec``
/ ``ManagedObjectReference`` / ``RetrieveOptions`` annotations (#3103).
The pre-#3103 substrate annotated only base-typed *polymorphic* fields
(device backings, ``ClusterConfigSpecEx``); the differential disproved
that premise -- the 8.0.x deserialiser demands the tag even where the
declared type equals the runtime type. 9.x accepts annotated bodies (it
is the spec'd format), so annotation is unconditional -- no version gate.

This module carries the two request shapes shared across the substrate:
the annotated :func:`vim_moref` and the annotated single-filter
``RetrievePropertiesEx`` body (:func:`retrieve_properties_body` -- the
``PropertyFilterSpec`` / ``PropertySpec`` / ``ObjectSpec`` trio plus
``RetrieveOptions``). Everything else stays an explicit per-builder
annotation at its call site (spec-groundable and reviewable); there is
deliberately no recursive type-inference magic here.

It also carries the **response-side twin** (#3106):
:func:`unwrap_vim_value`, the tolerant un-boxer every vim property
consumer funnels ``DynamicProperty.val`` (and ``TaskInfo``) reads
through. ``DynamicProperty.val`` is ``Any``-typed in the pinned
``vi-json.yaml``, and VI-JSON **boxes** values in ``Any`` placeholders:
a primitive arrives as ``{"_typeName": "string", "_value":
"dvportgroup-1766"}`` (live-observed on vCenter 8.0.3, #3106) and an
array as ``{"_typeName": "ArrayOfString", "_value": [...]}`` (every
``PrimitiveX`` / ``ArrayOfX`` component in the spec keys its payload
``_value``), while MoRefs / DataObjects arrive as plain
``_typeName``-annotated dicts -- no box. The pre-#3106 extractors read
``val`` as the bare value, so every primitive-typed property read
failed its type guard against live 8.0.x (proven: ``vm.create``'s DVPG
``key`` lookup returned 200 with the data present yet failed
``network_lookup``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MOREF_TYPE_NAME",
    "VIM_TYPE_NAME_KEY",
    "VIM_VALUE_KEY",
    "fault_message",
    "retrieve_properties_body",
    "unwrap_vim_value",
    "vim_moref",
]

#: The VI-JSON polymorphic-type discriminator key (``Any._typeName`` in
#: the pinned ``vi-json.yaml``; required on every DataObject).
VIM_TYPE_NAME_KEY: Final[str] = "_typeName"

#: The VI-JSON boxed-value payload key. Every ``PrimitiveX`` /
#: ``ArrayOfX`` component in the pinned ``vi-json.yaml`` -- the boxes
#: VI-JSON puts around primitives and arrays in ``Any`` placeholders
#: such as ``DynamicProperty.val`` -- names ``_value`` in ``required``.
VIM_VALUE_KEY: Final[str] = "_value"

#: ``_typeName`` prefix of the boxed-array components (``ArrayOfString``,
#: ``ArrayOfManagedObjectReference``, ``ArrayOfVirtualDevice``, ...).
_ARRAY_OF_PREFIX: Final[str] = "ArrayOf"

#: ``_typeName`` of a vim managed-object reference.
MOREF_TYPE_NAME: Final[str] = "ManagedObjectReference"

# ``_typeName`` values of the RetrievePropertiesEx body trio + options
# (spec-verified against the pinned ``vi-json.yaml``:
# ``RetrievePropertiesExRequestType.specSet`` items are
# ``PropertyFilterSpec``; its ``propSet`` / ``objectSet`` items are
# ``PropertySpec`` / ``ObjectSpec``; ``options`` is ``RetrieveOptions``).
_PROPERTY_FILTER_SPEC_TYPE: Final[str] = "PropertyFilterSpec"
_PROPERTY_SPEC_TYPE: Final[str] = "PropertySpec"
_OBJECT_SPEC_TYPE: Final[str] = "ObjectSpec"
_RETRIEVE_OPTIONS_TYPE: Final[str] = "RetrieveOptions"


def vim_moref(mo_type: str, moid: str) -> dict[str, str]:
    """An annotated vim ``ManagedObjectReference`` JSON object.

    ``{"_typeName": "ManagedObjectReference", "type": <T>, "value":
    <moid>}`` -- the request-body form every MoRef-typed field takes on
    the VI-JSON wire (#3103; response-side MoRefs carry the same tag).
    """
    return {VIM_TYPE_NAME_KEY: MOREF_TYPE_NAME, "type": mo_type, "value": moid}


def retrieve_properties_body(
    mo_type: str, moids: Sequence[str], path_set: Sequence[str]
) -> dict[str, Any]:
    """An annotated single-filter ``RetrievePropertiesEx`` request body.

    One ``PropertyFilterSpec`` scoped directly to the ``(mo_type, moid)``
    object(s) -- one ``ObjectSpec`` per moid, no ``TraversalSpec``, one
    ``PropertySpec`` naming *path_set* -- the shape every property read
    in the substrate sends (typed reads, config pre-reads, task polls).
    The singleton ``propertyCollector`` moId rides the request path, so
    the body is only the ``RetrievePropertiesExRequestType`` method args
    ``specSet`` + ``options``, with every DataObject carrying its
    ``_typeName`` discriminator -- the live-verified 200 shape from the
    #3103 differential.
    """
    return {
        "specSet": [
            {
                VIM_TYPE_NAME_KEY: _PROPERTY_FILTER_SPEC_TYPE,
                "propSet": [
                    {
                        VIM_TYPE_NAME_KEY: _PROPERTY_SPEC_TYPE,
                        "type": mo_type,
                        "pathSet": list(path_set),
                    }
                ],
                "objectSet": [
                    {VIM_TYPE_NAME_KEY: _OBJECT_SPEC_TYPE, "obj": vim_moref(mo_type, moid)}
                    for moid in moids
                ],
            }
        ],
        "options": {VIM_TYPE_NAME_KEY: _RETRIEVE_OPTIONS_TYPE},
    }


def unwrap_vim_value(value: Any) -> Any:
    """Recursively un-box VI-JSON ``Any``-placeholder values; plain values pass through.

    VI-JSON boxes what lands in an ``Any`` placeholder (``DynamicProperty.val``,
    ``TaskInfo.result``, ``ArrayOfAnyType`` elements):

    * a primitive as ``{"_typeName": "string", "_value": "dvportgroup-1766"}``
      (live-observed 8.0.3 shape, #3106; the ``PrimitiveBoolean`` / ``PrimitiveInt``
      / ... components of the pinned ``vi-json.yaml``),
    * an array as ``{"_typeName": "ArrayOfString", "_value": [...]}`` (every
      ``ArrayOfX`` component keys its payload ``_value`` too),

    while MoRefs and DataObjects arrive as plain ``_typeName``-annotated dicts.
    This helper strips the boxes and **only** the boxes -- ``_typeName`` tags on
    DataObjects survive (consumers key on them, e.g. the disk-grow
    ``VirtualDisk`` guard), and already-bare values return unchanged, so it is
    tolerant both ways (9.x / vcsim payloads that arrive un-boxed keep working).
    The walk recurses through nested containers so boxes in nested ``Any``
    positions (a ``TaskInfo.result`` primitive, ``ArrayOfAnyType`` elements)
    normalise in the same pass.

    A SOAP-flavoured boxed array that keys its payload by element type instead
    (``{"_typeName": "ArrayOfString", "string": [...]}`` -- the shape #3106's
    report cites) is tolerated as well: any ``ArrayOf*``-tagged dict whose
    single payload key holds a list unwraps to that list.

    Apply this at **response consumption** only -- never on a payload that
    round-trips into a request body (e.g. the resolved ``CustomizationSpec``
    a clone embeds), where a stripped box would corrupt an ``Any``-typed
    request field.
    """
    if isinstance(value, dict):
        if VIM_VALUE_KEY in value:
            return unwrap_vim_value(value[VIM_VALUE_KEY])
        type_name = value.get(VIM_TYPE_NAME_KEY)
        if isinstance(type_name, str) and type_name.startswith(_ARRAY_OF_PREFIX):
            payload = [v for k, v in value.items() if k != VIM_TYPE_NAME_KEY]
            if len(payload) == 1 and isinstance(payload[0], list):
                return unwrap_vim_value(payload[0])
        return {key: unwrap_vim_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unwrap_vim_value(item) for item in value]
    return value


def _join_fault_messages(fault: dict[str, Any]) -> str | None:
    """Join a fault DataObject's ``faultMessage[*].message`` texts, else ``None``.

    Each ``LocalizableMessage`` in ``faultMessage`` carries an optional
    per-message ``message``; blank / non-string entries are skipped and the
    surviving texts join with ``; `` (#3116).
    """
    messages = fault.get("faultMessage")
    if not isinstance(messages, list):
        return None
    texts = [
        message
        for entry in messages
        if isinstance(entry, dict)
        and isinstance(message := entry.get("message"), str)
        and message.strip()
    ]
    return "; ".join(texts) if texts else None


def _fault_type_name(fault: dict[str, Any]) -> str | None:
    """Return a fault DataObject's concrete ``_typeName`` when it is real text."""
    type_name = fault.get(VIM_TYPE_NAME_KEY)
    return type_name if isinstance(type_name, str) and type_name.strip() else None


def fault_message(error: Any) -> str | None:
    """Best human-readable text from a vim ``TaskInfo.error`` / ``MethodFault``.

    A faulted task's ``error`` (and the OVF / lease fault projections) arrive
    in one of two VI-JSON shapes, depending on the vCenter version's
    serializer:

    * **9.x** -- a ``LocalizedMethodFault``: an optional ``localizedMessage``
      plus the concrete ``fault`` DataObject
      (``fault.faultMessage[*].message`` / ``fault._typeName``).
    * **8.0.x** -- the concrete fault DataObject **flattened directly onto**
      ``error`` (``_typeName`` + ``faultstring`` [+ ``faultMessage``], no
      ``LocalizedMethodFault`` wrapper and no nested ``fault``). The 9.x chain
      read ``error.get("fault")``, found nothing, and discarded the fault
      text, so operators saw ``<no fault reported>`` even though vCenter had
      reported an actionable fault (#3663).

    Extraction order:

    1. ``error.localizedMessage`` -- the server-localized text, when present
       (9.x). Returned verbatim, preserving the historical shape (#3116).
    2. nested ``error.fault`` present (9.x ``LocalizedMethodFault``): the
       joined ``fault.faultMessage[*].message``, else the concrete
       ``fault._typeName`` -- verbatim, as before (#3116).
    3. otherwise ``error`` *is* the flattened fault (8.0.x): its
       ``faultstring`` / joined ``faultMessage[*].message`` prefixed with the
       concrete ``_typeName`` so the operator sees both (e.g.
       ``InvalidArgument: A specified parameter was not correct:
       configSpec.guestId``); the bare ``_typeName`` when the flattened fault
       carries a type but no text (#3663).

    Returns ``None`` only when ``error`` is not a dict or carries no usable
    fault content at all. Callers funnel ``error`` through
    :func:`unwrap_vim_value` first, so boxed nested primitives (#3106) are
    already bare here.
    """
    if not isinstance(error, dict):
        return None
    localized = error.get("localizedMessage")
    if isinstance(localized, str) and localized.strip():
        return localized
    nested = error.get("fault")
    if isinstance(nested, dict):
        # 9.x LocalizedMethodFault: the concrete fault is nested. Preserve the
        # historical bare-text output (#3116) -- no type-name prefix.
        joined = _join_fault_messages(nested)
        if joined is not None:
            return joined
        return _fault_type_name(nested)
    # 8.0.x flattened VI-JSON: ``error`` itself is the concrete fault (#3663).
    type_name = _fault_type_name(error)
    faultstring = error.get("faultstring")
    if isinstance(faultstring, str) and faultstring.strip():
        text: str | None = faultstring
    else:
        text = _join_fault_messages(error)
    if text is not None:
        return f"{type_name}: {text}" if type_name is not None else text
    return type_name
