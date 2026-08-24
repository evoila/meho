# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared ``_typeName``-annotated vim (VI-JSON) request-body pieces (#3103).

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

This module carries the two shapes shared across the substrate: the
annotated :func:`vim_moref` and the annotated single-filter
``RetrievePropertiesEx`` body (:func:`retrieve_properties_body` -- the
``PropertyFilterSpec`` / ``PropertySpec`` / ``ObjectSpec`` trio plus
``RetrieveOptions``). Everything else stays an explicit per-builder
annotation at its call site (spec-groundable and reviewable); there is
deliberately no recursive type-inference magic here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MOREF_TYPE_NAME",
    "VIM_TYPE_NAME_KEY",
    "retrieve_properties_body",
    "vim_moref",
]

#: The VI-JSON polymorphic-type discriminator key (``Any._typeName`` in
#: the pinned ``vi-json.yaml``; required on every DataObject).
VIM_TYPE_NAME_KEY: Final[str] = "_typeName"

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
