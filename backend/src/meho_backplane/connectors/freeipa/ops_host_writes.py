# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the FreeIPA host writes: host groups, their members, automember rules, deletes.

Every write waits until a second person approves. A change is ``caution``, a
delete is ``dangerous``. The bodies live in
:mod:`meho_backplane.connectors.freeipa.writes`.
"""

from __future__ import annotations

from typing import Any, Literal

from meho_backplane.connectors.freeipa._op_common import (
    GROUP_HOSTS,
    UNTRUSTED_TEXT_NOTE,
    WRITE_TAGS,
    FreeipaTypedOp,
    name_schema,
)
from meho_backplane.connectors.freeipa.writes import HOST_DELETE_OP_ID

__all__ = ["HOST_WRITE_OPS"]

_APPROVAL_NOTE = "A second person must approve the call."

_DESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 1024,
    "description": "Optional. A short text that says what it is for.",
}


def _names(description: str, item: str) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "uniqueItems": True,
        "description": description,
        "items": name_schema(item),
    }


def _entry(description: str) -> dict[str, Any]:
    return {"type": "object", "description": description}


_COMPLETED: dict[str, Any] = {
    "type": "integer",
    "description": "How many values FreeIPA added: all that were sent, or the call fails.",
}

_DELETE_RESPONSE: dict[str, Any] = {
    "deleted": {
        "type": "array",
        "description": "The names FreeIPA deleted.",
        "items": {"type": "string", "description": "One deleted name."},
    },
    "warnings": {
        "type": "array",
        "description": "FreeIPA's warnings, for example that it found no DNS records to remove.",
        "items": {"type": "string", "description": "One warning."},
    },
}


def _write_op(
    *,
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    example: dict[str, Any],
    response: dict[str, Any],
    tags: tuple[str, ...],
    safety_level: Literal["caution", "dangerous"],
    llm_instructions: dict[str, Any],
    any_of: list[dict[str, Any]] | None = None,
) -> FreeipaTypedOp:
    """A write in the ``hosts`` group. It always waits until a second person approves."""
    parameter_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        "examples": [example],
    }
    if any_of:
        parameter_schema["anyOf"] = any_of
    return FreeipaTypedOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        parameter_schema=parameter_schema,
        response_schema={"type": "object", "properties": response},
        group_key=GROUP_HOSTS,
        tags=(*WRITE_TAGS, *tags),
        safety_level=safety_level,
        requires_approval=True,
        llm_instructions=llm_instructions,
    )


HOST_WRITE_OPS: tuple[FreeipaTypedOp, ...] = (
    _write_op(
        op_id="freeipa.hostgroup.create",
        handler_attr="hostgroup_create",
        summary=(
            "Create an empty host group in the IPA domain. Waits until a second person approves."
        ),
        description=(
            "Runs the IPA command hostgroup_add. The new host group has no members. Returns the "
            "new host group."
        ),
        properties={
            "name": name_schema(
                "The new host group's name, for example webservers. FreeIPA stores it in "
                "lower case."
            ),
            "description": _DESCRIPTION_SCHEMA,
        },
        required=["name"],
        example={"name": "webservers", "description": "Web servers of the lab"},
        response={"entry": _entry("The new host group, with FreeIPA's default attributes.")},
        tags=("hostgroup",),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to add a new, empty host group to the IPA domain, for example before you "
                "add hosts to it or make an automember rule for it."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} No host group with this name may exist; check with "
                "freeipa.hostgroup.list."
            ),
            "output_shape": "{entry}: the new host group; cn holds its name.",
            "next_step": (
                "To add hosts, call freeipa.hostgroup.member.add. To let FreeIPA add new hosts "
                "by their name, call freeipa.automember.rule.create. A DuplicateEntry error means "
                "the group exists already."
            ),
        },
    ),
    _write_op(
        op_id="freeipa.hostgroup.member.add",
        handler_attr="hostgroup_member_add",
        summary=(
            "Add existing hosts or host groups to a host group in the IPA domain. Waits until a "
            "second person approves."
        ),
        description=(
            "Runs the IPA command hostgroup_add_member. Never creates a host. Fails and names each "
            "member FreeIPA did not add, for example a host that does not exist."
        ),
        properties={
            "name": name_schema("The host group's name."),
            "hosts": _names(
                "Hosts to add, by their fully qualified names. Each host must exist.",
                "One host's fully qualified name, for example web01.example.test.",
            ),
            "hostgroups": _names(
                "Host groups to add as members of this host group, by name.",
                "One host group's name.",
            ),
        },
        required=["name"],
        any_of=[{"required": ["hosts"]}, {"required": ["hostgroups"]}],
        example={"name": "webservers", "hosts": ["web01.example.test", "web02.example.test"]},
        response={
            "entry": _entry(
                "The host group after the change, with its members in member_host and "
                "member_hostgroup."
            ),
            "completed": _COMPLETED,
        },
        tags=("hostgroup", "host", "membership"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to put existing hosts or host groups into a host group. It changes the "
                "group's members. It never creates a host."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} The host group and every member must exist. Give hosts, "
                "hostgroups or both."
            ),
            "output_shape": (
                "{entry, completed}: the host group after the change, and how many members "
                "FreeIPA added. When FreeIPA did not add a member, the call fails and names each "
                "one with FreeIPA's reason, for example 'no such entry' or 'This entry is already "
                "a member'. The other members were added."
            ),
            "next_step": f"Check the members with freeipa.hostgroup.show. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    _write_op(
        op_id="freeipa.automember.rule.create",
        handler_attr="automember_rule_create",
        summary=(
            "Create an automember rule that puts new hosts into a host group. Waits until a second "
            "person approves."
        ),
        description=(
            "Runs the IPA command automember_add with type hostgroup. The rule gets the host "
            "group's name and has no conditions yet. Returns the new rule."
        ),
        properties={
            "hostgroup": name_schema(
                "The host group the rule fills. The rule gets the same name; a host group has at "
                "most one rule."
            ),
            "description": _DESCRIPTION_SCHEMA,
        },
        required=["hostgroup"],
        example={"hostgroup": "webservers", "description": "Web servers by host name"},
        response={
            "entry": _entry(
                "The new rule: cn is the host group's name, automembertargetgroup the group's "
                "LDAP name (DN)."
            ),
        },
        tags=("automember", "hostgroup"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to make FreeIPA put new hosts into a host group by itself, for example "
                "every new web server into webservers. It adds a rule to the IPA domain."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} The host group must exist (freeipa.hostgroup.create) and must "
                "not have a rule yet."
            ),
            "output_shape": "{entry}: the new rule with FreeIPA's default attributes.",
            "next_step": (
                "The rule has no conditions yet, so it matches no host. Add one with "
                "freeipa.automember.condition.add."
            ),
        },
    ),
    _write_op(
        op_id="freeipa.automember.condition.add",
        handler_attr="automember_condition_add",
        summary=(
            "Add conditions to a host group's automember rule: which new hosts it puts into the "
            "group. Waits for approval."
        ),
        description=(
            "Runs the IPA command automember_add_condition with type hostgroup. A new host whose "
            "key attribute matches an inclusive regex, and no exclusive one, joins the group. "
            "Fails when FreeIPA adds fewer conditions than sent."
        ),
        properties={
            "hostgroup": name_schema("The host group whose rule gets the conditions."),
            "key": name_schema(
                "The host attribute the regular expressions are matched against, for example "
                "fqdn (the host's full name)."
            ),
            "inclusive_regex": _names(
                "Regular expressions. A new host whose key matches one of them joins the group.",
                "One regular expression, for example ^web.",
            ),
            "exclusive_regex": _names(
                "Regular expressions. A new host whose key matches one of them never joins, also "
                "when an inclusive one matches.",
                "One regular expression, for example ^webtest.",
            ),
        },
        required=["hostgroup", "key"],
        any_of=[{"required": ["inclusive_regex"]}, {"required": ["exclusive_regex"]}],
        example={"hostgroup": "webservers", "key": "fqdn", "inclusive_regex": ["^web"]},
        response={
            "entry": _entry(
                "The rule after the change. Its conditions are in automemberinclusiveregex and "
                "automemberexclusiveregex, each as <key>=<regex>."
            ),
            "completed": _COMPLETED,
        },
        tags=("automember", "hostgroup"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call after freeipa.automember.rule.create, to say which new hosts the rule puts "
                "into its host group. It changes the rule's conditions."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} The rule must exist. FreeIPA checks that key is an attribute "
                "it knows. Give inclusive_regex, exclusive_regex or both."
            ),
            "output_shape": (
                "{entry, completed}: the rule after the change, and how many conditions FreeIPA "
                "added. When FreeIPA added fewer than you sent, the call fails: a condition the "
                "rule already has is not added again."
            ),
            "next_step": (
                "The rule acts on hosts that are added to FreeIPA after it. To check one host, "
                f"read its host groups with freeipa.host.show. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    _write_op(
        op_id=HOST_DELETE_OP_ID,
        handler_attr="host_delete",
        summary=(
            "Delete a host from the IPA domain. Waits until a second person approves; the approval "
            "shows its host groups."
        ),
        description=(
            "Runs the IPA command host_del for one host. FreeIPA also deletes the host's services "
            "and revokes its certificates, so the machine can no longer use IPA. With updatedns, "
            "it also removes the host's records from IPA's DNS."
        ),
        properties={
            "fqdn": name_schema("The host's fully qualified name, for example web01.example.test."),
            "updatedns": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Optional, default false. true: also remove the host's A, AAAA, SSHFP and PTR "
                    "records from IPA's own DNS."
                ),
            },
        },
        required=["fqdn"],
        example={"fqdn": "web01.example.test"},
        response=_DELETE_RESPONSE,
        tags=("host", "enrollment"),
        safety_level="dangerous",
        llm_instructions={
            "when_to_call": (
                "Call to remove a host from the IPA domain, for example after its machine was "
                "torn down. It deletes the host entry; the host leaves every host group and rule."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} The approval shows the host and its host groups. Make sure the "
                "machine is gone or will join again: a joined machine can no longer log users in "
                "through IPA. FreeIPA refuses to delete an IPA server."
            ),
            "output_shape": "{deleted, warnings}: the deleted names, and FreeIPA's warnings.",
            "next_step": (
                "A NotFound error means no such host exists. To join the machine again, it needs "
                "a new host entry."
            ),
        },
    ),
    _write_op(
        op_id="freeipa.hostgroup.delete",
        handler_attr="hostgroup_delete",
        summary=(
            "Delete a host group from the IPA domain; its hosts stay. Waits until a second person "
            "approves."
        ),
        description=(
            "Runs the IPA command hostgroup_del for one host group. The member hosts stay in the "
            "domain. Sudo rules and access rules that name the group lose it. FreeIPA refuses to "
            "delete the group ipaservers."
        ),
        properties={"name": name_schema("The host group's name.")},
        required=["name"],
        example={"name": "webservers"},
        response=_DELETE_RESPONSE,
        tags=("hostgroup",),
        safety_level="dangerous",
        llm_instructions={
            "when_to_call": (
                "Call to remove a host group that is no longer needed. It deletes the group; its "
                "hosts only leave it."
            ),
            "preconditions": (
                f"{_APPROVAL_NOTE} Read the group first with freeipa.hostgroup.show. Check "
                "which sudo rules and access rules use it (freeipa.sudorule.list, "
                "freeipa.hbacrule.list): they lose the group, so its hosts may lose access."
            ),
            "output_shape": "{deleted, warnings}: the deleted names, and FreeIPA's warnings.",
            "next_step": "A NotFound error means no such host group exists.",
        },
    ),
)
