# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cloud templates and catalog sources: one op set for two Automation connectors.

Aria Automation 8 (connector ``vcfa-vra8-8.0``) and the tenant plane of
VCF Automation 9 (connector ``vcfa-rest-9.0``, "VM Apps" organisations) serve
the same blueprint and catalog APIs. So the eleven ops are written once:

* :class:`ContentOpSpec` holds the metadata of one op. Its texts use
  ``{ns}`` where the op id prefix goes; :func:`render_text` puts in
  ``vcfa.tenant`` or ``vcfa.vra8``.
* :class:`VcfaContentOpsMixin` holds the handlers. Both connector classes
  inherit it, so the dispatcher binds the handler to whichever connector the
  target resolves to.
* :data:`CONTENT_GROUP_TEXTS` holds the two group texts.

The op list itself is in :mod:`.vcfa_content_ops`; each connector turns it
into its own op type and registers it under its own ids and group keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from meho_backplane.connectors.vmware_shared import vcfa_catalog, vcfa_templates

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "CONTENT_GROUP_TEXTS",
    "ContentOpSpec",
    "VcfaContentOpsMixin",
    "change_answer",
    "field",
    "flat_page",
    "guidance",
    "list_of",
    "nullable",
    "obj",
    "render_text",
]

#: The text marker for the op id prefix of a connector.
NS_MARKER: Final[str] = "{ns}"


@dataclass(frozen=True)
class ContentOpSpec:
    """One op, before a connector gives it an id prefix and a group key.

    ``name`` is the op id after the prefix (``blueprint.get``). ``group`` is
    ``"templates"`` or ``"catalog"``. ``path`` is the main vendor path.
    """

    name: str
    handler_attr: str
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    group: Literal["templates", "catalog"]
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any]


def render_text(value: Any, ns: str) -> Any:
    """Put the op id prefix *ns* in for ``{ns}`` in every string of *value*."""
    if isinstance(value, str):
        return value.replace(NS_MARKER, ns)
    if isinstance(value, dict):
        return {key: render_text(item, ns) for key, item in value.items()}
    if isinstance(value, list):
        return [render_text(item, ns) for item in value]
    if isinstance(value, tuple):
        return tuple(render_text(item, ns) for item in value)
    return value


#: Group text per logical group. Each connector renders ``{ns}``.
CONTENT_GROUP_TEXTS: Final[dict[str, str]] = {
    "templates": (
        "Cloud templates (blueprints) of a project. Reads: one template with its YAML "
        "and inputs ({ns}.blueprint.get), the versions of a template and which are "
        "released ({ns}.blueprint.version.list), and a check of template YAML that "
        "changes nothing ({ns}.blueprint.validate). Changes: create a template "
        "({ns}.blueprint.create), change its YAML ({ns}.blueprint.update), delete it "
        "({ns}.blueprint.delete), and release or unrelease a version "
        "({ns}.blueprint.version.release). A person must approve each change."
    ),
    "catalog": (
        "Catalog content sources and sharing. Reads: the content sources "
        "({ns}.catalog.source.list) and what is shared with which project "
        "({ns}.catalog.share.list). Changes: add a source that imports the cloud "
        "templates of a project ({ns}.catalog.source.create), and share a source or "
        "one item with a project, or stop sharing it ({ns}.catalog.share.set). A "
        "person must approve each change."
    ),
}


# ---------------------------------------------------------------------------
# Small schema helpers
# ---------------------------------------------------------------------------


def field(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description, **extra}


def obj(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """An open object schema that names its main fields."""
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def list_of(description: str, item_properties: dict[str, Any]) -> dict[str, Any]:
    """An array of open objects that name their main fields."""
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": item_properties, "additionalProperties": True},
    }


def guidance(
    *,
    when_to_call: str,
    output_shape: str,
    next_step: str,
    preconditions: str | None = None,
) -> dict[str, Any]:
    """The ``llm_instructions`` blob. ``preconditions`` is set on every change."""
    blob = {"when_to_call": when_to_call, "output_shape": output_shape, "next_step": next_step}
    if preconditions is not None:
        blob["preconditions"] = preconditions
    return blob


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """*schema*, but it may also be null."""
    return {**schema, "type": ["object", "null"]}


def change_answer(
    statuses: list[str],
    key: str,
    item: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The answer schema of a change: ``{status, <key>, guidance}`` plus *extra*."""
    return {
        "type": "object",
        "properties": {
            "status": field("string", f"What the op did: {', '.join(statuses)}.", enum=statuses),
            key: item,
            **(extra or {}),
            "guidance": field("string", "A short note on what to do next."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    }


def flat_page(what: str, item: dict[str, Any]) -> dict[str, Any]:
    """The answer schema of a list: ``{content: [rows], totalElements}``."""
    return obj(
        f"A flat list of {what}.",
        {
            "content": list_of(f"One row per {what.removesuffix('s')}.", item),
            "totalElements": field("integer", f"The total number of {what}."),
        },
    )


# ---------------------------------------------------------------------------
# Handlers, inherited by both connector classes
# ---------------------------------------------------------------------------


class VcfaContentOpsMixin:
    """The handlers of the template and catalog ops.

    ``self`` is the connector. It has ``_request_json`` and ``_post_json``
    (:class:`~.vcfa_templates.ContentTransport`) and its own login.
    """

    async def _content_get(
        self: vcfa_templates.ContentTransport,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """A ``GET`` with ``Accept: application/json``; the answer may be a list."""
        return await self._request_json(
            target,
            "GET",
            path,
            operator=operator,
            params=params,
            extra_headers=dict(vcfa_templates.JSON_ACCEPT),
        )

    async def vcfa_blueprint_get(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.get``."""
        return await vcfa_templates.blueprint_get(self, operator, target, params)

    async def vcfa_blueprint_create(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.create``."""
        return await vcfa_templates.blueprint_create(self, operator, target, params)

    async def vcfa_blueprint_update(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.update``."""
        return await vcfa_templates.blueprint_update(self, operator, target, params)

    async def vcfa_blueprint_delete(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.delete``."""
        return await vcfa_templates.blueprint_delete(self, operator, target, params)

    async def vcfa_blueprint_version_list(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.version.list``."""
        return await vcfa_templates.blueprint_version_list(self, operator, target, params)

    async def vcfa_blueprint_version_release(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.version.release``."""
        return await vcfa_templates.blueprint_version_release(self, operator, target, params)

    async def vcfa_blueprint_validate(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.blueprint.validate``."""
        return await vcfa_templates.blueprint_validate(self, operator, target, params)

    async def vcfa_catalog_source_list(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.catalog.source.list``."""
        return await vcfa_catalog.catalog_source_list(self, operator, target, params)

    async def vcfa_catalog_source_create(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.catalog.source.create``."""
        return await vcfa_catalog.catalog_source_create(self, operator, target, params)

    async def vcfa_catalog_share_list(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.catalog.share.list``."""
        return await vcfa_catalog.catalog_share_list(self, operator, target, params)

    async def vcfa_catalog_share_set(
        self: vcfa_templates.ContentTransport,
        operator: Operator,
        target: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """``*.catalog.share.set``."""
        return await vcfa_catalog.catalog_share_set(self, operator, target, params)
