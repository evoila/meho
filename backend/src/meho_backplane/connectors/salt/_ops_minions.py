# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Salt server and minion reads: about, minions, keys, saved targets."""

from __future__ import annotations

from meho_backplane.connectors.salt._op_common import (
    GROUP_MINIONS,
    GROUP_SERVER,
    KEY_ROW_PROPERTIES,
    KEY_STATE_SCHEMA,
    MASTER_ID_SCHEMA,
    MINION_ID_SCHEMA,
    PAGE_SCHEMA,
    TAGS,
    SaltTypedOp,
    _limit,
    _text,
)
from meho_backplane.connectors.salt.reads import JOIN_LIMIT, KEY_PAGE_LIMIT, LIST_PAGE_LIMIT

__all__ = ["ABOUT_OP", "KEY_LIST_OP", "MINION_LIST_OP", "TARGET_LIST_OP"]

ABOUT_OP = SaltTypedOp(
    op_id="salt.about",
    handler_attr="about",
    summary="Read the Salt RaaS version, the versions of its parts and the masters it knows.",
    description=(
        "Calls the RaaS RPC methods api.get_versions and master.get_master_presence. "
        "Returns the RaaS version, the Python version, the version of each part, and "
        "each master with its status and last contact. MEHO uses the same version read "
        "to detect the Salt version."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "raas_version": _text("RaaS version number, for example 8.18.0."),
            "raas_version_text": _text("RaaS version text as the server reports it."),
            "python_version": _text("Python version of the RaaS server."),
            "components": {
                "type": "array",
                "description": "Version of each RaaS part.",
                "items": {
                    "type": "object",
                    "description": "One part.",
                    "properties": {
                        "name": {"type": "string", "description": "Part name."},
                        "version": {"type": "string", "description": "Part version."},
                    },
                },
            },
            "master_count": {"type": "integer", "description": "How many masters RaaS knows."},
            "masters": {
                "type": "array",
                "description": "The masters, at most 100.",
                "items": {
                    "type": "object",
                    "description": "One master.",
                    "properties": {
                        "master_id": _text("Master id."),
                        "cluster_id": _text("Cluster id of the master, if any."),
                        "status": _text("Presence status of the master."),
                        "last_seen": _text("Last contact time (ISO 8601)."),
                    },
                },
            },
        },
    },
    group_key=GROUP_SERVER,
    tags=(*TAGS, "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a Salt target, to confirm the RaaS version (8.x or 9.1) and to "
            "see which Salt masters are connected."
        ),
        "output_shape": (
            "{raas_version, raas_version_text, python_version, components: [{name, "
            "version}], master_count, masters: [{master_id, cluster_id, status, last_seen}]}."
        ),
        "next_step": (
            "If a master's status is not present, report it. To see the managed machines, "
            "call salt.minion.list; to see recent jobs, call salt.job.list."
        ),
    },
)

MINION_LIST_OP = SaltTypedOp(
    op_id="salt.minion.list",
    handler_attr="minion_list",
    summary="List Salt minions with key state, master, presence, last contact and OS.",
    description=(
        "Reads one page of minion keys (minions.get_minion_key_state) and joins each "
        "minion's presence (minions.get_minion_presence) and OS grains "
        "(minions.get_minion_cache). One flat row per minion."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "master_id": MASTER_ID_SCHEMA,
            "minion_id": MINION_ID_SCHEMA,
            "key_state": KEY_STATE_SCHEMA,
            "limit": _limit(KEY_PAGE_LIMIT, 1000),
            "page": PAGE_SCHEMA,
        },
        "additionalProperties": False,
        "examples": [{"key_state": "accepted", "limit": 100}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "How many minions match, all pages."},
            "details_complete": {
                "type": "boolean",
                "description": (
                    f"False when there are more than {JOIN_LIMIT} minions, so presence and "
                    "OS may be null for some rows."
                ),
            },
            "minions": {
                "type": "array",
                "description": "One row per minion, sorted by minion id.",
                "items": {
                    "type": "object",
                    "description": "One minion.",
                    "properties": {
                        **KEY_ROW_PROPERTIES,
                        "presence": _text("Presence status, for example present or lost."),
                        "last_seen": _text("Last contact time (ISO 8601)."),
                        "os": _text("OS name (grain os)."),
                        "os_release": _text("OS release (grain osrelease)."),
                        "kernel": _text("Kernel (grain kernel)."),
                        "salt_version": _text("Salt version on the minion (grain saltversion)."),
                    },
                },
            },
        },
    },
    group_key=GROUP_MINIONS,
    tags=(*TAGS, "minion"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which machines Salt manages, which are present or lost, when each "
            "was last seen and which OS it runs."
        ),
        "output_shape": (
            "{count, details_complete, minions: [{minion_id, master_id, key_state, presence, "
            "last_seen, os, os_release, kernel, salt_version}]}. A long list is behind a "
            "result handle; read it with result_query."
        ),
        "next_step": (
            "A minion with presence lost has not talked to its master: report it. A "
            "pending key waits for a person to accept it. If details_complete is false, "
            "pass master_id or minion_id to narrow the read. Use count with limit and page "
            "to read further pages."
        ),
        "result_scalars": {"keys": ["count", "details_complete"]},
    },
)

KEY_LIST_OP = SaltTypedOp(
    op_id="salt.key.list",
    handler_attr="key_list",
    summary="List Salt minion keys by state: accepted, pending, rejected or denied.",
    description=(
        "Calls minions.get_minion_key_state. One row per minion key: minion id, master "
        "and key state. Filter by key_state to find keys that wait to be accepted."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "master_id": MASTER_ID_SCHEMA,
            "minion_id": MINION_ID_SCHEMA,
            "key_state": KEY_STATE_SCHEMA,
            "limit": _limit(KEY_PAGE_LIMIT, 1000),
            "page": PAGE_SCHEMA,
        },
        "additionalProperties": False,
        "examples": [{"key_state": "pending"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "How many keys match, all pages."},
            "keys": {
                "type": "array",
                "description": "One row per minion key, sorted by minion id.",
                "items": {
                    "type": "object",
                    "description": "One minion key.",
                    "properties": KEY_ROW_PROPERTIES,
                },
            },
        },
    },
    group_key=GROUP_MINIONS,
    # Not tagged "key": that word marks an operation that sends a secret
    # (SECRET_FAMILY_TAGS), so a trace would keep no body and an error would
    # lose the vendor's text. A minion key row holds no secret.
    tags=(*TAGS, "minion", "acceptance"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the minion keys of a state, for example the pending keys of new "
            "machines, or the rejected and denied keys."
        ),
        "output_shape": (
            "{count, keys: [{minion_id, master_id, key_state}]}. A long list is behind a "
            "result handle; read it with result_query."
        ),
        "next_step": (
            "Report pending keys: a person must check and accept them before Salt manages "
            "the machine. For presence and OS, call salt.minion.list."
        ),
        "result_scalars": {"keys": ["count"]},
    },
)

TARGET_LIST_OP = SaltTypedOp(
    op_id="salt.target.list",
    handler_attr="target_list",
    summary="List saved Salt targets (minion groups) and what each one matches.",
    description=(
        "Calls tgt.get_target_group without pillar data. One row per saved target: id, "
        "name, description and, per master, the match type and the match text."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Only saved targets with this name.",
            },
            "master_id": MASTER_ID_SCHEMA,
            "limit": _limit(LIST_PAGE_LIMIT, 500),
            "page": PAGE_SCHEMA,
        },
        "additionalProperties": False,
        "examples": [{"name": "<target-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "How many saved targets match."},
            "targets": {
                "type": "array",
                "description": "One row per saved target.",
                "items": {
                    "type": "object",
                    "description": "One saved target.",
                    "properties": {
                        "uuid": _text("Id of the saved target."),
                        "name": _text("Name."),
                        "desc": _text("Description."),
                        "targets": {
                            "type": "array",
                            "description": "What the target matches, per master.",
                            "items": {
                                "type": "object",
                                "description": "The match on one master.",
                                "properties": {
                                    "master_id": {
                                        "type": "string",
                                        "description": "Master id; * means all masters.",
                                    },
                                    "tgt_type": _text("Match type, for example glob or grain."),
                                    "tgt": {
                                        "type": ["string", "array", "null"],
                                        "description": "Match text or list of minions.",
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    group_key=GROUP_MINIONS,
    tags=(*TAGS, "target"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the saved targets (minion groups) that jobs run on, or to find "
            "which minions a target name matches."
        ),
        "output_shape": (
            "{count, targets: [{uuid, name, desc, targets: [{master_id, tgt_type, tgt}]}]}. "
            "Pillar data is not read. A long list is behind a result handle."
        ),
        "next_step": (
            "To see the runs on a target, call salt.job.list and look at tgt_name. To see "
            "the minions, call salt.minion.list."
        ),
        "result_scalars": {"keys": ["count"]},
    },
)
