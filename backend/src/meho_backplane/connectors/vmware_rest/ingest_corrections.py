# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingest-time corrections for the VMware REST catalogs.

**Summaries (#4329).** The vSphere REST spec (``vcenter.yaml``) gives no
``summary`` to any operation, so search shows none. An operation with no
summary gets the first sentence of its description, cut to 120
characters. The VI-JSON spec (``vi-json.yaml``) repeats one summary for
the same method on many managed object types; a VI-JSON operation whose
summary another operation of the same spec also has gets the object type
in front (``VirtualMachine: Destroys this object, ...``).

**Tasks-list body (#3700).**
``POST /cis/tasks?action=list`` (list vCenter tasks). On the 8.0 catalog the
ingested request body comes out flat: ``exclude_result`` and ``return_all``
at the top of ``body``. A vCenter 8.0.3 server rejects that shape with HTTP
400. It wants the two-level body that the 9.0 spec already describes::

    {"filter_spec": {...}, "result_spec": {...}}

The 8.0.3 server also rejects an empty filter: at least one of
``filter_spec.services`` or ``filter_spec.tasks`` must be a non-empty list.
This module replaces the ingested body schema with that shape, so MEHO
refuses a wrong body before it reaches vCenter, and adds agent guidance
that names the mandatory filter.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.ingest.spec_corrections import (
    register_ingest_batch_correction,
    register_ingest_spec_correction,
)

__all__ = [
    "MAX_SUMMARY_CHARS",
    "TASKS_LIST_OP_PATH",
    "summary_from_description",
    "vmware_rest_batch_correction",
    "vmware_rest_spec_correction",
]

#: Catalog versions whose ingested tasks-list body is corrected. The 9.0
#: spec already models the body correctly, so only 8.0 is listed.
_CORRECTED_VERSIONS = frozenset({"8.0"})

#: Spec-relative path of the tasks-list op (the ``/api`` mount is stripped
#: before the compare, so both op_id spellings match).
TASKS_LIST_OP_PATH = "/cis/tasks?action=list"

#: Longest summary made from a description.
MAX_SUMMARY_CHARS = 120

_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
#: VI-JSON descriptions put privileges and "since" notes in paragraphs that
#: start like this; they are not a summary.
_METADATA_PREFIX = "***"

#: A VI-JSON path: ``/<Type>/{moId}/<method or property>``, with an optional
#: lower-case service segment in front (``/vsan/...``, ``/pbm/...``). No
#: vSphere REST (``vcenter.yaml``) path has this shape.
_VI_JSON_PATH_RE = re.compile(r"^(?:/[a-z]+)?/(?P<type>[A-Z][A-Za-z0-9]*)/\{moId\}(?:/|$)")

_STRING_SET: dict[str, Any] = {"type": "array", "uniqueItems": True, "items": {"type": "string"}}

#: The ``{filter_spec, result_spec}`` body, field names as in the vendor
#: 9.0 ``Cis.Tasks.FilterSpec`` / ``Cis.Tasks.GetSpec`` schemas. Inline (no
#: ``$ref``) so the schema stays valid on its own. ``additionalProperties:
#: false`` on the body rejects the old flat shape; the ``anyOf`` rejects an
#: empty filter, which vCenter 8.0.3 answers with HTTP 400.
_TASKS_LIST_BODY: dict[str, Any] = {
    "type": "object",
    "description": (
        "Request body for listing tasks: {filter_spec, result_spec}. "
        "filter_spec is required and must name at least one service or task."
    ),
    "properties": {
        "filter_spec": {
            "type": "object",
            "description": (
                "Which tasks to list. Set a non-empty `services` or `tasks` list; "
                "vCenter 8.0 rejects an empty filter with HTTP 400."
            ),
            "properties": {
                "services": {
                    **_STRING_SET,
                    "description": "Service ids, for example com.vmware.vcenter.vm.",
                },
                "tasks": {**_STRING_SET, "description": "Task ids."},
                "operations": {
                    **_STRING_SET,
                    "description": "Operation ids. Also set `services` when you use this.",
                },
                "status": {
                    **_STRING_SET,
                    "description": "PENDING, RUNNING, BLOCKED, SUCCEEDED, or FAILED.",
                },
                "targets": {
                    "type": "array",
                    "description": "Resources the tasks worked on.",
                    "items": {
                        "type": "object",
                        "properties": {"type": {"type": "string"}, "id": {"type": "string"}},
                        "required": ["type", "id"],
                    },
                },
                "users": {**_STRING_SET, "description": "Users who started the tasks."},
            },
            "anyOf": [
                {"required": ["services"], "properties": {"services": {"minItems": 1}}},
                {"required": ["tasks"], "properties": {"tasks": {"minItems": 1}}},
            ],
        },
        "result_spec": {
            "type": "object",
            "description": "What to return for each task. Optional.",
            "properties": {
                "return_all": {
                    "type": "boolean",
                    "description": "Return all data, including operation-specific data.",
                },
                "exclude_result": {
                    "type": "boolean",
                    "description": "Leave out the operation result.",
                },
            },
        },
    },
    "required": ["filter_spec"],
    "additionalProperties": False,
    "x-meho-param-loc": "body",
}

_TASKS_LIST_LLM_INSTRUCTIONS: dict[str, object] = {
    "when_to_use": (
        "List vCenter tasks by service or task id, for example to check "
        "whether recent operations of one service finished or failed."
    ),
    "parameter_hints": {
        "body": (
            "Send body={filter_spec: {...}, result_spec: {...}}. filter_spec "
            "must have a non-empty `services` or `tasks` list: vCenter 8.0 "
            "answers an empty filter with HTTP 400. result_spec "
            "(return_all, exclude_result) is optional. Do not put "
            "return_all or exclude_result at the top of body."
        ),
    },
    "output_shape": "Map of task id to task info (status, service, operation, ...).",
}


def _is_tasks_list(proto: EndpointDescriptorProto) -> bool:
    return proto.method.upper() == "POST" and proto.path.removeprefix("/api") == TASKS_LIST_OP_PATH


def summary_from_description(description: str | None) -> str | None:
    """Return the first sentence of *description*, at most 120 characters.

    Paragraphs that only carry vendor metadata (``***Required privileges:***
    ...``, ``***Since:*** ...``) are skipped. A first sentence longer than the
    limit is cut at a word and ends with an ellipsis. ``None`` when no prose
    is left.
    """
    if not description:
        return None
    text = ""
    for paragraph in _PARAGRAPH_BREAK_RE.split(description):
        text = " ".join(paragraph.split())
        if text and not text.startswith(_METADATA_PREFIX):
            break
    else:
        return None
    end = _SENTENCE_END_RE.search(text)
    sentence = text[: end.end()] if end else text
    if len(sentence) <= MAX_SUMMARY_CHARS:
        return sentence
    head = sentence[: MAX_SUMMARY_CHARS - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "\u2026"


def _correct_tasks_list(proto: EndpointDescriptorProto) -> EndpointDescriptorProto:
    schema: dict[str, object] = dict(proto.parameter_schema)
    raw_properties = schema.get("properties")
    properties = dict(raw_properties) if isinstance(raw_properties, dict) else {}
    properties["body"] = copy.deepcopy(_TASKS_LIST_BODY)
    schema["properties"] = properties
    raw_required = schema.get("required")
    required = [n for n in raw_required if n != "body"] if isinstance(raw_required, list) else []
    schema["required"] = [*required, "body"]
    return proto.model_copy(
        update={
            "parameter_schema": schema,
            "llm_instructions": copy.deepcopy(_TASKS_LIST_LLM_INSTRUCTIONS),
        }
    )


def vmware_rest_spec_correction(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Fill a missing summary; give the 8.0 tasks-list op its two-level body."""
    if version in _CORRECTED_VERSIONS and _is_tasks_list(proto):
        proto = _correct_tasks_list(proto)
    if proto.summary and proto.summary.strip():
        return proto
    summary = summary_from_description(proto.description)
    return proto if summary is None else proto.model_copy(update={"summary": summary})


def _summary_key(summary: str | None) -> str:
    return " ".join(summary.split()) if summary else ""


def vmware_rest_batch_correction(
    version: str, protos: Sequence[EndpointDescriptorProto]
) -> tuple[EndpointDescriptorProto, ...]:
    """Put the managed object type in front of a shared VI-JSON summary."""
    del version  # The VI-JSON summaries repeat the same way on every version.
    types: dict[int, str] = {}
    for index, proto in enumerate(protos):
        match = _VI_JSON_PATH_RE.match(proto.path)
        if match is not None and _summary_key(proto.summary):
            types[index] = match.group("type")
    counts = Counter(_summary_key(protos[index].summary) for index in types)
    corrected: list[EndpointDescriptorProto] = []
    for index, proto in enumerate(protos):
        object_type = types.get(index)
        key = _summary_key(proto.summary)
        if object_type is None or counts[key] < 2:
            corrected.append(proto)
        else:
            corrected.append(proto.model_copy(update={"summary": f"{object_type}: {key}"}))
    return tuple(corrected)


register_ingest_spec_correction(
    product="vmware",
    impl_id="vmware-rest",
    correction=vmware_rest_spec_correction,
)
register_ingest_batch_correction(
    product="vmware",
    impl_id="vmware-rest",
    correction=vmware_rest_batch_correction,
)
