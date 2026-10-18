# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor advertised by the VMware REST generic connector."""

from __future__ import annotations

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Versioned catalogs whose ingested VM writes carry this floor. Both the 9.0
# (#3563/#3564) and the 8.0 U3 (#3569) generic catalogs share the same
# hardware-mutating VM write families, so the floor applies identically. The
# floor is registered once per ``(product, impl_id)``; this set scopes it to
# the qualified catalog versions rather than every future version.
_FLOORED_VERSIONS = frozenset({"8.0", "9.0"})


# Tag detach is a POST with an ``?action=detach...`` query in the path key
# (``detach``, ``detach-multiple-tags-from-object``,
# ``detach-tag-from-multiple-objects``). It removes tags from inventory
# objects, so it waits for approval like the tag and category DELETEs (#3827).
_TAG_ASSOCIATION_PATH = "/cis/tagging/tag-association"
_TAG_DETACH_ACTION_PREFIX = "action=detach"


def _is_tag_detach(method: str, canonical_path: str, query: str) -> bool:
    """Identify the vCenter tag-association detach actions."""
    if method != "POST" or not query.startswith(_TAG_DETACH_ACTION_PREFIX):
        return False
    return canonical_path == _TAG_ASSOCIATION_PATH or canonical_path.startswith(
        _TAG_ASSOCIATION_PATH + "/"
    )


def _requires_vmware_safety_floor(method: str, path: str) -> bool:
    """Identify VMware write families that require approval."""
    if method not in _WRITE_METHODS:
        return False
    raw_path, _, query = path.partition("?")
    canonical_path = raw_path.removeprefix("/api")
    if _is_tag_detach(method, canonical_path, query):
        return True
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
    """Promote VM hardware, lifecycle, power, and tag-detach writes for the VMware catalogs."""
    if version not in _FLOORED_VERSIONS or not _requires_vmware_safety_floor(
        proto.method.upper(), proto.path
    ):
        return proto
    safety_level = proto.safety_level if proto.safety_level == "destructive" else "dangerous"
    return proto.model_copy(update={"safety_level": safety_level, "requires_approval": True})


register_ingest_safety_floor(
    product="vmware",
    impl_id="vmware-rest",
    floor=vmware_rest_safety_floor,
)
