# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the FreeIPA user, group and role writes.

Every write waits until a second person approves. A change is ``caution``, a
delete is ``dangerous``. Adding members to a role is ``dangerous`` too, because
a role's members get all of its rights. The bodies live in
:mod:`meho_backplane.connectors.freeipa.identity_writes`.

No operation here takes or returns a password, and none carries the tag
``password``: with it, MEHO would treat the op as one that sends a secret and
hide FreeIPA's error text.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.freeipa._op_common import (
    APPROVAL_NOTE,
    COMPLETED_SCHEMA,
    DESCRIPTION_SCHEMA,
    GROUP_IDENTITY,
    UNTRUSTED_TEXT_NOTE,
    FreeipaTypedOp,
    entry_schema,
    name_schema,
    names_schema,
    write_op,
)
from meho_backplane.connectors.freeipa.identity_writes import (
    ROLE_DELETE_OP_ID,
    ROLE_MEMBER_ADD_OP_ID,
    USER_DELETE_OP_ID,
)

__all__ = ["IDENTITY_WRITE_OPS"]

_LOGIN_SCHEMA: dict[str, Any] = name_schema("The user's login name, for example jdoe.")

_DELETE_RESPONSE: dict[str, Any] = {
    "deleted": {
        "type": "array",
        "description": "The names FreeIPA deleted.",
        "items": {"type": "string", "description": "One deleted name."},
    },
    "warnings": {
        "type": "array",
        "description": "FreeIPA's warnings.",
        "items": {"type": "string", "description": "One warning."},
    },
}

_EXPIRES_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
    "description": (
        "When the user's password runs out: a UTC time such as 2030-01-01T00:00:00Z. It must "
        "be in the future."
    ),
}

_ROLE_MEMBER_PROPERTIES: dict[str, Any] = {
    "name": name_schema("The role's name, for example Enrollment Administrator."),
    "users": names_schema("Users to add, by login name. Each user must exist.", "One login name."),
    "groups": names_schema(
        "User groups to add, by name. Each group must exist.", "One user group's name."
    ),
    "hosts": names_schema(
        "Hosts to add, by their fully qualified names. Each host must exist.",
        "One host's fully qualified name, for example web01.example.test.",
    ),
    "hostgroups": names_schema(
        "Host groups to add, by name. Each host group must exist.", "One host group's name."
    ),
}


IDENTITY_WRITE_OPS: tuple[FreeipaTypedOp, ...] = (
    write_op(
        group_key=GROUP_IDENTITY,
        op_id="freeipa.user.create",
        handler_attr="user_create",
        summary=(
            "Create a user in the IPA domain. The user has no password yet. Waits until a second "
            "person approves."
        ),
        description=(
            "Runs the IPA command user_add. It sends no password and does not ask FreeIPA for a "
            "random one, so the new user cannot log in until an administrator sets a password. "
            "Returns the new user."
        ),
        properties={
            "login": name_schema("The new user's login name, for example jdoe."),
            "first_name": name_schema("The user's first name."),
            "last_name": name_schema("The user's last name."),
            "mail": name_schema("Optional. The user's e-mail address."),
        },
        required=["login", "first_name", "last_name"],
        example={"login": "jdoe", "first_name": "Jane", "last_name": "Doe"},
        response={"entry": entry_schema("The new user, with FreeIPA's default attributes.")},
        tags=("user",),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to add a new user to the IPA domain, for example a service account or a "
                "new colleague. It adds an account to the IPA domain."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} No user with this login may exist; check with freeipa.user.list. "
                "This operation takes no password and never returns one."
            ),
            "output_shape": "{entry}: the new user; uid holds the login name.",
            "next_step": (
                "To put the user in groups, call freeipa.group.member.add. For a service "
                "account, call freeipa.user.password_expiry.set. A DuplicateEntry error means "
                f"the user exists already. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id="freeipa.user.password_expiry.set",
        handler_attr="user_password_expiry_set",
        summary=(
            "Set the date when a user's password runs out. Waits until a second person approves."
        ),
        description=(
            "Runs the IPA command user_mod with the option krbpasswordexpiration. It changes only "
            "the date. It does not set or change a password. Fails when the date is not in the "
            "future."
        ),
        properties={"login": _LOGIN_SCHEMA, "expires": _EXPIRES_SCHEMA},
        required=["login", "expires"],
        example={"login": "svc-backup", "expires": "2030-01-01T00:00:00Z"},
        response={"entry": entry_schema("The user after the change, with FreeIPA's attributes.")},
        tags=("user", "expiry"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to change when a user's password runs out, for example to give a service "
                "account an expiry date far in the future. A password that an administrator "
                "sets runs out at once, so this is how a service account keeps its password."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The user must exist. The date must be in the future, in UTC."
            ),
            "output_shape": "{entry}: the user after the change.",
            "next_step": (
                "Check the date with freeipa.user.show. A NotFound error means the user does not "
                f"exist. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id="freeipa.user.disable",
        handler_attr="user_disable",
        summary="Disable a user in the IPA domain, so the user cannot log in. Waits for approval.",
        description=(
            "Runs the IPA command user_disable. The user and the user's groups stay. FreeIPA "
            "fails when the user is disabled already."
        ),
        properties={"login": _LOGIN_SCHEMA},
        required=["login"],
        example={"login": "jdoe"},
        response={
            "disabled": {"type": "string", "description": "The login name of the disabled user."}
        },
        tags=("user",),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to stop a user from logging in without deleting the user, for example when "
                "a colleague leaves."
            ),
            "preconditions": f"{APPROVAL_NOTE} The user must exist and be enabled.",
            "output_shape": "{disabled}: the login name of the disabled user.",
            "next_step": (
                "Check the result with freeipa.user.show; nsaccountlock is true for a disabled "
                f"user. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id="freeipa.group.create",
        handler_attr="group_create",
        summary=(
            "Create an empty user group in the IPA domain. Waits until a second person approves."
        ),
        description=(
            "Runs the IPA command group_add. The new group has no members. Returns the new group."
        ),
        properties={
            "name": name_schema("The new group's name, for example webadmins."),
            "description": DESCRIPTION_SCHEMA,
        },
        required=["name"],
        example={"name": "webadmins", "description": "Administrators of the web servers"},
        response={"entry": entry_schema("The new group, with FreeIPA's default attributes.")},
        tags=("group",),
        safety_level="caution",
        llm_instructions={
            "when_to_call": "Call to add a new, empty user group to the IPA domain.",
            "preconditions": (
                f"{APPROVAL_NOTE} No group with this name may exist; check with freeipa.group.list."
            ),
            "output_shape": "{entry}: the new group; cn holds its name.",
            "next_step": (
                "To add users, call freeipa.group.member.add. A DuplicateEntry error means the "
                f"group exists already. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id="freeipa.group.member.add",
        handler_attr="group_member_add",
        summary=(
            "Add existing users or user groups to a user group in the IPA domain. Waits until a "
            "second person approves."
        ),
        description=(
            "Runs the IPA command group_add_member. Never creates a user. Fails and names each "
            "member FreeIPA did not add, for example a user that does not exist."
        ),
        properties={
            "name": name_schema("The group's name."),
            "users": names_schema(
                "Users to add, by login name. Each user must exist.", "One login name."
            ),
            "groups": names_schema(
                "User groups to add as members of this group, by name.", "One user group's name."
            ),
        },
        required=["name"],
        any_of=[{"required": ["users"]}, {"required": ["groups"]}],
        example={"name": "webadmins", "users": ["jdoe"]},
        response={
            "entry": entry_schema(
                "The group after the change, with its members in member_user and member_group."
            ),
            "completed": COMPLETED_SCHEMA,
        },
        tags=("group", "user", "membership"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to put existing users or user groups into a user group. Members get the "
                "rights the group has."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The group and every member must exist. Give users, groups or "
                "both."
            ),
            "output_shape": (
                "{entry, completed}: the group after the change, and how many members FreeIPA "
                "added. When FreeIPA did not add a member, the call fails and names each one with "
                "FreeIPA's reason, for example 'no such entry' or 'This entry is already a "
                "member'. The other members were added."
            ),
            "next_step": f"Check the members with freeipa.group.show. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id=ROLE_MEMBER_ADD_OP_ID,
        handler_attr="role_member_add",
        summary=(
            "Add existing users, groups or hosts to a role; they get its rights. Waits for approval"
        ),
        description=(
            "Runs the IPA command role_add_member. A member of a role gets all privileges of "
            "the role, for example the right to add hosts. Never creates a member. Fails and "
            "names each member FreeIPA did not add."
        ),
        properties=_ROLE_MEMBER_PROPERTIES,
        required=["name"],
        any_of=[
            {"required": ["users"]},
            {"required": ["groups"]},
            {"required": ["hosts"]},
            {"required": ["hostgroups"]},
        ],
        example={"name": "Enrollment Administrator", "users": ["svc-join"]},
        response={
            "entry": entry_schema(
                "The role after the change, with its members in member_user, member_group, "
                "member_host and member_hostgroup."
            ),
            "completed": COMPLETED_SCHEMA,
        },
        tags=("role", "membership", "rights"),
        safety_level="dangerous",
        llm_instructions={
            "when_to_call": (
                "Call to give users, user groups, hosts or host groups the rights of a role. "
                "This gives new rights, so a second person checks it."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The role and every member must exist. Read the role with "
                "freeipa.role.show first, to see which privileges the members get."
            ),
            "output_shape": (
                "{entry, completed}: the role after the change, and how many members FreeIPA "
                "added. When FreeIPA did not add a member, the call fails and names each one "
                "with FreeIPA's reason. The other members were added."
            ),
            "next_step": f"Check the members with freeipa.role.show. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id=USER_DELETE_OP_ID,
        handler_attr="user_delete",
        summary="Delete a user from the IPA domain for good. Waits until a second person approves.",
        description=(
            "Runs the IPA command user_del. The user is deleted, not kept. The user also "
            "leaves all groups and roles. Fails when the user does not exist."
        ),
        properties={"login": _LOGIN_SCHEMA},
        required=["login"],
        example={"login": "jdoe"},
        response=_DELETE_RESPONSE,
        tags=("user", "delete"),
        safety_level="dangerous",
        llm_instructions={
            "when_to_call": (
                "Call to remove a user from the IPA domain for good. To only stop a login, call "
                "freeipa.user.disable instead."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The approver sees the groups and roles the user leaves. FreeIPA "
                "refuses to delete the last administrator."
            ),
            "output_shape": "{deleted, warnings}: the deleted login names and FreeIPA's warnings.",
            "next_step": f"Check with freeipa.user.list. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    write_op(
        group_key=GROUP_IDENTITY,
        op_id=ROLE_DELETE_OP_ID,
        handler_attr="role_delete",
        summary="Delete a role from the IPA domain. Waits until a second person approves.",
        description=(
            "Runs the IPA command role_del. Its members lose the rights the role gave them. "
            "Fails when the role does not exist."
        ),
        properties={"name": name_schema("The role's name.")},
        required=["name"],
        example={"name": "Enrollment Administrator"},
        response=_DELETE_RESPONSE,
        tags=("role", "delete"),
        safety_level="dangerous",
        llm_instructions={
            "when_to_call": "Call to remove a role from the IPA domain.",
            "preconditions": (
                f"{APPROVAL_NOTE} The approver sees the members and privileges of the role."
            ),
            "output_shape": "{deleted, warnings}: the deleted role names and FreeIPA's warnings.",
            "next_step": f"Check with freeipa.role.list. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
)
