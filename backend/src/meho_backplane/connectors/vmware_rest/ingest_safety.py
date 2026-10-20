# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor advertised by the VMware REST generic connector."""

from __future__ import annotations

import re

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = [
    "is_vmware_library_item_delete",
    "is_vmware_tagging_removal",
    "vmware_rest_safety_floor",
]

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Versioned catalogs whose ingested VM writes carry this floor. Both the 9.0
# (#3563/#3564) and the 8.0 U3 (#3569) generic catalogs share the same
# hardware-mutating VM write families, so the floor applies identically. The
# floor is registered once per ``(product, impl_id)``; this set scopes it to
# the qualified catalog versions rather than every future version.
_FLOORED_VERSIONS = frozenset({"8.0", "9.0"})


# The tagging removals are ``destructive`` (#3827): a category delete also
# removes every tag in it and detaches those tags from every object; a tag
# delete detaches it everywhere; a detach drops the association that
# governance (for example storage policy tags) relies on. ``destructive``
# means every seat waits for a second person, a standing grant can never
# satisfy the op, and the park needs a preview with the affected objects.
# The preview builders live in ``tagging_delete_preview.py``.
_TAG_ASSOCIATION_PATH = "/cis/tagging/tag-association"
_TAG_DETACH_ACTION_PREFIX = "action=detach"
_TAGGING_OBJECT_PATH_RE = re.compile(r"/cis/tagging/(category|tag)/\{[^/{}]+\}")

# A content-library item delete is ``destructive`` too (#4058): the item and
# its files are gone for good, like a deleted VM. The preview builder lives in
# ``library_item_delete_preview.py``; it reuses the planner of the governed
# ``vmware.composite.content_library.item.delete``.
_LIBRARY_ITEM_PATH_RE = re.compile(r"/content/library/item/\{[^/{}]+\}")


def _canonical(path: str) -> tuple[str, str]:
    """Split *path* into ``(path without the /api mount, query)``."""
    raw_path, _, query = path.partition("?")
    return raw_path.removeprefix("/api"), query


def _is_tag_detach(method: str, canonical_path: str, query: str) -> bool:
    """Identify the vCenter tag-association detach actions."""
    if method != "POST" or not query.startswith(_TAG_DETACH_ACTION_PREFIX):
        return False
    return canonical_path == _TAG_ASSOCIATION_PATH or canonical_path.startswith(
        _TAG_ASSOCIATION_PATH + "/"
    )


def is_vmware_tagging_removal(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` deletes a tag or category, or detaches a tag.

    Matches ``DELETE /cis/tagging/category/{id}``, ``DELETE /cis/tagging/tag/{id}``
    and the three ``POST /cis/tagging/tag-association[/{id}]?action=detach*``
    actions, with or without the ``/api`` mount in the path.
    """
    method = method.upper()
    canonical_path, query = _canonical(path)
    if method == "DELETE":
        return _TAGGING_OBJECT_PATH_RE.fullmatch(canonical_path) is not None
    return _is_tag_detach(method, canonical_path, query)


def is_vmware_library_item_delete(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is ``DELETE /content/library/item/{id}``.

    Matches with or without the ``/api`` mount in the path.
    """
    canonical_path, query = _canonical(path)
    return (
        method.upper() == "DELETE"
        and not query
        and _LIBRARY_ITEM_PATH_RE.fullmatch(canonical_path) is not None
    )


def _requires_vmware_safety_floor(method: str, path: str) -> bool:
    """Identify VMware VM write families that require approval."""
    if method not in _WRITE_METHODS:
        return False
    canonical_path, _query = _canonical(path)
    if canonical_path == "/vcenter/vm/{vm}/hardware":
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/hardware/"):
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/power/"):
        return True
    return (
        (method == "POST" and canonical_path == "/vcenter/vm")
        or (method == "DELETE" and canonical_path == "/vcenter/vm/{vm}")
        or canonical_path == "/vcenter/vm/{vm}/power"
    )


def vmware_rest_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Promote VM, tagging-removal, and library-item-delete writes for the VMware catalogs."""
    if version not in _FLOORED_VERSIONS:
        return proto
    method = proto.method.upper()
    if is_vmware_tagging_removal(method, proto.path) or is_vmware_library_item_delete(
        method, proto.path
    ):
        return proto.model_copy(update={"safety_level": "destructive", "requires_approval": True})
    if not _requires_vmware_safety_floor(method, proto.path):
        return proto
    safety_level = proto.safety_level if proto.safety_level == "destructive" else "dangerous"
    return proto.model_copy(update={"safety_level": safety_level, "requires_approval": True})


register_ingest_safety_floor(
    product="vmware",
    impl_id="vmware-rest",
    floor=vmware_rest_safety_floor,
)
