# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the FreeIPA server, host and identity reads."""

from __future__ import annotations

from meho_backplane.connectors.freeipa._op_common import (
    GROUP_HOSTS,
    GROUP_IDENTITY,
    GROUP_SERVER,
    TAGS,
    UNTRUSTED_TEXT_NOTE,
    FreeipaTypedOp,
    list_op,
    name_schema,
    show_op,
)

__all__ = ["DIRECTORY_OPS"]


def _text(description: str) -> dict[str, object]:
    return {"type": ["string", "null"], "description": description}


ABOUT_OP = FreeipaTypedOp(
    op_id="freeipa.about",
    handler_attr="about",
    summary="Read the FreeIPA server version and API version, and which IPA user MEHO logs in as.",
    description=(
        "Runs the IPA command ping. Returns the server version, the server's API version, the "
        "API version MEHO sends, and the IPA principal of MEHO's login. MEHO uses the same "
        "command to detect the version."
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
            "server_version": _text("FreeIPA server version, for example 4.13.4."),
            "api_version": _text("API version of the server, for example 2.257."),
            "connector_api_version": {
                "type": "string",
                "description": "API version MEHO sends with every command.",
            },
            "principal": _text("Kerberos principal of the IPA user MEHO logged in as."),
        },
    },
    group_key=GROUP_SERVER,
    tags=(*TAGS, "probe", "version"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a FreeIPA target, to check that MEHO can log in and to see the "
            "server version."
        ),
        "output_shape": "{server_version, api_version, connector_api_version, principal}.",
        "next_step": (
            "For hosts, call freeipa.host.list; for users, freeipa.user.list; for access rules, "
            "freeipa.hbacrule.list."
        ),
    },
)

DIRECTORY_OPS: tuple[FreeipaTypedOp, ...] = (
    ABOUT_OP,
    list_op(
        op_id="freeipa.host.list",
        handler_attr="host_list",
        summary="List the hosts of the IPA domain, optionally only those in one host group.",
        description=(
            "Runs the IPA command host_find. Filters: text (matches the host name, description "
            "and other main fields) and hostgroup (only members of that host group). Returns one "
            "row per host."
        ),
        group_key=GROUP_HOSTS,
        tags=("host", "hostgroup", "enrollment"),
        row="one host",
        when_to_call=(
            "Call to find hosts by name, or to list the hosts of one host group, for example "
            "after machines joined the domain."
        ),
        next_step="For the full record of one host, call freeipa.host.show with its fqdn.",
        extra_properties={
            "hostgroup": name_schema("Only hosts that are members of this host group (its name).")
        },
        example={"hostgroup": "webservers", "limit": 50},
    ),
    show_op(
        op_id="freeipa.host.show",
        handler_attr="host_show",
        summary="Read one host of the IPA domain with its host groups and other attributes.",
        description="Runs the IPA command host_show for one host, picked by its full name.",
        group_key=GROUP_HOSTS,
        tags=("host", "enrollment"),
        name_key="fqdn",
        name_description="The host's fully qualified name, for example web01.example.test.",
        example_name="web01.example.test",
        entry="the host",
        when_to_call=(
            "Call when you know a host's name and need its record, for example which host groups "
            "it is in."
        ),
        next_step=(
            "To check who may log in to it, call freeipa.hbac.test with a user and a service."
        ),
    ),
    list_op(
        op_id="freeipa.hostgroup.list",
        handler_attr="hostgroup_list",
        summary="List the host groups of the IPA domain.",
        description=(
            "Runs the IPA command hostgroup_find. Filter: text (matches the name and the "
            "description). Returns one row per host group."
        ),
        group_key=GROUP_HOSTS,
        tags=("hostgroup",),
        row="one host group",
        when_to_call="Call to find a host group by name, or to see which host groups exist.",
        next_step=(
            "For the hosts of one group, call freeipa.host.list with hostgroup, or "
            "freeipa.hostgroup.show."
        ),
    ),
    show_op(
        op_id="freeipa.hostgroup.show",
        handler_attr="hostgroup_show",
        summary="Read one host group of the IPA domain with its member hosts and host groups.",
        description="Runs the IPA command hostgroup_show for one host group, picked by its name.",
        group_key=GROUP_HOSTS,
        tags=("hostgroup", "membership"),
        name_key="name",
        name_description="The host group's name.",
        example_name="webservers",
        entry="the host group, with its member hosts and member host groups",
        when_to_call="Call when you know a host group's name and need its members.",
        next_step="For full host records, call freeipa.host.list with hostgroup set to this group.",
    ),
    list_op(
        op_id="freeipa.user.list",
        handler_attr="user_list",
        summary="List the users of the IPA domain.",
        description=(
            "Runs the IPA command user_find. Filter: text (matches the login, the names and the "
            "e-mail address). Returns one row per active user; preserved (deleted but kept) "
            "users are not listed."
        ),
        group_key=GROUP_IDENTITY,
        tags=("user", "account"),
        row="one user",
        when_to_call="Call to find a user by login, name or e-mail address, or to list users.",
        next_step="For one user's full record with their groups, call freeipa.user.show.",
        example={"text": "jdoe", "limit": 20},
    ),
    show_op(
        op_id="freeipa.user.show",
        handler_attr="user_show",
        summary="Read one user of the IPA domain with the groups and roles they are a member of.",
        description="Runs the IPA command user_show for one user, picked by the login name.",
        group_key=GROUP_IDENTITY,
        tags=("user", "account", "membership"),
        name_key="uid",
        name_description="The user's login name.",
        example_name="jdoe",
        entry="the user, with the groups and roles they are a member of",
        when_to_call=(
            "Call when you know a login name and need the user's record, for example its groups "
            "or whether the account is disabled."
        ),
        next_step=(
            "To check where the user may log in, call freeipa.hbac.test with a host and a service."
        ),
    ),
    list_op(
        op_id="freeipa.group.list",
        handler_attr="group_list",
        summary="List the user groups of the IPA domain.",
        description=(
            "Runs the IPA command group_find. Filter: text (matches the name and the "
            "description). Returns one row per user group."
        ),
        group_key=GROUP_IDENTITY,
        tags=("group", "membership"),
        row="one user group",
        when_to_call="Call to find a user group by name, or to see which groups exist.",
        next_step="For the members of one group, call freeipa.group.show.",
    ),
    show_op(
        op_id="freeipa.group.show",
        handler_attr="group_show",
        summary="Read one user group of the IPA domain with its member users and groups.",
        description="Runs the IPA command group_show for one user group, picked by its name.",
        group_key=GROUP_IDENTITY,
        tags=("group", "membership"),
        name_key="name",
        name_description="The group's name.",
        example_name="admins",
        entry="the user group, with its member users and member groups",
        when_to_call="Call when you know a group's name and need its members.",
        next_step="For one member's full record, call freeipa.user.show with its login name.",
    ),
    list_op(
        op_id="freeipa.role.list",
        handler_attr="role_list",
        summary="List the roles of the IPA domain.",
        description=(
            "Runs the IPA command role_find. Filter: text (matches the name and the "
            "description). Returns one row per role. A role gives its members a set of "
            "privileges."
        ),
        group_key=GROUP_IDENTITY,
        tags=("role", "privilege", "rbac"),
        row="one role",
        when_to_call=(
            "Call to see which roles exist, for example to find a role for host enrollment."
        ),
        next_step="For the members and privileges of one role, call freeipa.role.show.",
    ),
    show_op(
        op_id="freeipa.role.show",
        handler_attr="role_show",
        summary="Read one role of the IPA domain with its members and privileges.",
        description="Runs the IPA command role_show for one role, picked by its name.",
        group_key=GROUP_IDENTITY,
        tags=("role", "privilege", "rbac", "membership"),
        name_key="name",
        name_description="The role's name, for example Enrollment Administrator.",
        example_name="Enrollment Administrator",
        entry="the role, with its members and privileges",
        when_to_call="Call when you need to know who holds a role and what the role allows.",
        next_step="For one member's record, call freeipa.user.show or freeipa.group.show.",
    ),
    FreeipaTypedOp(
        op_id="freeipa.pwpolicy.show",
        handler_attr="pwpolicy_show",
        summary="Read the password policy of a user group, or the global password policy.",
        description=(
            "Runs the IPA command pwpolicy_show. Without group, it reads the global policy, "
            "which applies to every user whose groups have no policy of their own. Returns the "
            "policy's settings, such as password lifetime, minimum length and lockout."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "group": name_schema(
                    "The user group whose policy to read. Leave it out for the global policy."
                ),
            },
            "additionalProperties": False,
            "examples": [{}, {"group": "admins"}],
        },
        response_schema={
            "type": "object",
            "properties": {
                "entry": {
                    "type": "object",
                    "description": "The password policy, with FreeIPA's default attributes.",
                },
            },
        },
        group_key=GROUP_IDENTITY,
        tags=(*TAGS, "password", "policy"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to see the password rules: how long a password lasts, how long it must "
                "be, and when an account locks."
            ),
            "output_shape": (
                "{entry}: the policy with FreeIPA's own attribute names, mostly krb* keys such "
                "as krbmaxpwdlife; most values are lists, also with one value."
            ),
            "next_step": (
                "A NotFound error for a group means the group or its own policy does not exist; "
                "then call again without group to read the global policy. "
                f"{UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
)
