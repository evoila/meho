# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the FreeIPA sudo rule and access rule (HBAC) reads."""

from __future__ import annotations

from meho_backplane.connectors.freeipa._op_common import (
    GROUP_ACCESS,
    TAGS,
    UNTRUSTED_TEXT_NOTE,
    FreeipaTypedOp,
    list_op,
    name_schema,
    show_op,
)

__all__ = ["ACCESS_OPS"]


def _rule_names(description: str) -> dict[str, object]:
    return {"type": "array", "description": description, "items": {"type": "string"}}


HBAC_TEST_OP = FreeipaTypedOp(
    op_id="freeipa.hbac.test",
    handler_attr="hbac_test",
    summary="Test whether a user may use a service on a host under the IPA access rules (HBAC).",
    description=(
        "Runs the IPA command hbactest for one user, host and service. Returns whether access "
        "is granted and which enabled rules matched and did not match. Changes nothing."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "user": name_schema("The user's login name."),
            "host": name_schema("The host's fully qualified name."),
            "service": name_schema("The service name as an HBAC service, for example sshd."),
            "rules": {
                "type": "array",
                "minItems": 1,
                "description": "Test only these rules (their names). Leave it out to test all.",
                "items": {"type": "string", "minLength": 1, "description": "One rule name."},
            },
        },
        "required": ["user", "host", "service"],
        "additionalProperties": False,
        "examples": [{"user": "jdoe", "host": "web01.example.test", "service": "sshd"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "granted": {"type": "boolean", "description": "True when access is granted."},
            "matched": _rule_names("Rules that grant this access."),
            "notmatched": _rule_names("Rules that were checked and do not grant it."),
            "warnings": _rule_names("FreeIPA's warnings, for example unknown rule names."),
            "errors": _rule_names("FreeIPA's errors for the test, if any."),
        },
    },
    group_key=GROUP_ACCESS,
    tags=(*TAGS, "hbac", "access", "test"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to answer 'may this user log in to this host' (service sshd) or use another "
            "service there, before or after changing access rules."
        ),
        "output_shape": (
            "{granted, matched, notmatched, warnings, errors}; the lists hold rule names."
        ),
        "next_step": (
            "To see why a rule matches, call freeipa.hbacrule.show with its name. "
            f"{UNTRUSTED_TEXT_NOTE}"
        ),
    },
)

ACCESS_OPS: tuple[FreeipaTypedOp, ...] = (
    list_op(
        op_id="freeipa.sudorule.list",
        handler_attr="sudorule_list",
        summary="List the sudo rules of the IPA domain.",
        description=(
            "Runs the IPA command sudorule_find. Filter: text (matches the name and the "
            "description). Returns one row per sudo rule, enabled or not."
        ),
        group_key=GROUP_ACCESS,
        tags=("sudorule", "sudo-rule"),
        row="one sudo rule",
        when_to_call="Call to see which sudo rules exist, or to find one by name.",
        next_step="For who may run which commands on which hosts, call freeipa.sudorule.show.",
    ),
    show_op(
        op_id="freeipa.sudorule.show",
        handler_attr="sudorule_show",
        summary="Read one sudo rule of the IPA domain: its users, hosts and commands.",
        description="Runs the IPA command sudorule_show for one sudo rule, picked by its name.",
        group_key=GROUP_ACCESS,
        tags=("sudorule", "sudo-rule"),
        name_key="name",
        name_description="The sudo rule's name.",
        example_name="admins-all",
        entry="the sudo rule, with its users, hosts, commands and whether it is enabled",
        when_to_call="Call when you need to know who may run which commands with sudo, and where.",
        next_step=(
            "For the members of a user group or host group in the rule, call "
            "freeipa.group.show or freeipa.hostgroup.show."
        ),
    ),
    list_op(
        op_id="freeipa.hbacrule.list",
        handler_attr="hbacrule_list",
        summary="List the access rules (HBAC) of the IPA domain.",
        description=(
            "Runs the IPA command hbacrule_find. Filter: text (matches the name and the "
            "description). Returns one row per access rule, enabled or not, allow_all included."
        ),
        group_key=GROUP_ACCESS,
        tags=("hbac", "access"),
        row="one access rule",
        when_to_call=(
            "Call to see which access rules exist, for example whether the default rule "
            "allow_all is still enabled."
        ),
        next_step="To test one user, host and service against the rules, call freeipa.hbac.test.",
    ),
    show_op(
        op_id="freeipa.hbacrule.show",
        handler_attr="hbacrule_show",
        summary="Read one access rule (HBAC) of the IPA domain: its users, hosts and services.",
        description="Runs the IPA command hbacrule_show for one access rule, picked by its name.",
        group_key=GROUP_ACCESS,
        tags=("hbac", "access"),
        name_key="name",
        name_description="The access rule's name, for example allow_all.",
        example_name="allow_all",
        entry="the access rule, with its users, hosts, services and whether it is enabled",
        when_to_call="Call when you need to know whom an access rule lets in, and where.",
        next_step="To test the rule for one user, host and service, call freeipa.hbac.test.",
    ),
    HBAC_TEST_OP,
)
