# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the FreeIPA sudo, access rule (HBAC) and password policy writes.

Every write waits until a second person approves; each is ``caution``. The
bodies are methods of
:class:`~meho_backplane.connectors.freeipa.access_writes.FreeipaAccessWriteOps`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.freeipa._op_common import (
    APPROVAL_NOTE,
    COMPLETED_SCHEMA,
    DESCRIPTION_SCHEMA,
    GROUP_ACCESS,
    GROUP_IDENTITY,
    UNTRUSTED_TEXT_NOTE,
    FreeipaTypedOp,
    entry_schema,
    name_schema,
    names_schema,
    write_op,
)
from meho_backplane.connectors.freeipa.access_writes import (
    HBACRULE_DISABLE_OP_ID,
    PWPOLICY_OPTIONS,
    PWPOLICY_SET_OP_ID,
)

__all__ = ["ACCESS_WRITE_OPS"]

_SUDO_TAGS = ("sudorule", "sudo-rule")
_HBAC_TAGS = ("hbac", "access")

_NOT_ADDED = (
    "When FreeIPA did not add a member, the call fails and names each one with FreeIPA's reason, "
    "for example 'no such entry' or 'This entry is already a member'. The other members were "
    "added."
)

_USERS = names_schema("Users to add, by login name.", "One user's login name, for example jdoe.")
_GROUPS = names_schema("User groups to add, by name.", "One user group's name.")
_HOSTS = names_schema(
    "Hosts to add, by their fully qualified names.",
    "One host's fully qualified name, for example web01.example.test.",
)
_HOSTGROUPS = names_schema("Host groups to add, by name.", "One host group's name.")

_EXTERNAL: dict[str, Any] = {
    "type": "array",
    "description": (
        "Names FreeIPA does not know as IPA objects. It keeps them in the rule as external names, "
        "for example a local user. Empty when every name is in IPA."
    ),
    "items": {"type": "string", "description": "One external name."},
}


def _create_op(
    *,
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    name_key: str,
    name_description: str,
    entry: str,
    example: dict[str, Any],
    tags: tuple[str, ...],
    llm_instructions: dict[str, Any],
) -> FreeipaTypedOp:
    """A write that creates one object, picked by *name_key*, with an optional description."""
    return write_op(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        group_key=GROUP_ACCESS,
        properties={name_key: name_schema(name_description), "description": DESCRIPTION_SCHEMA},
        required=[name_key],
        example=example,
        response={"entry": entry_schema(entry)},
        tags=tags,
        safety_level="caution",
        llm_instructions=llm_instructions,
    )


def _member_add_op(
    *,
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    name_description: str,
    entry: str,
    members: dict[str, dict[str, Any]],
    example: dict[str, Any],
    tags: tuple[str, ...],
    llm_instructions: dict[str, Any],
    external: bool = False,
) -> FreeipaTypedOp:
    """A write that adds members to the rule ``name``.

    With one member parameter, it is required; with more, at least one of them is.
    """
    response = {
        "entry": entry_schema(entry),
        "completed": COMPLETED_SCHEMA,
    }
    if external:
        response["external"] = _EXTERNAL
    return write_op(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        group_key=GROUP_ACCESS,
        properties={"name": name_schema(name_description), **members},
        required=["name", *members] if len(members) == 1 else ["name"],
        any_of=[{"required": [key]} for key in members] if len(members) > 1 else None,
        example=example,
        response=response,
        tags=tags,
        safety_level="caution",
        llm_instructions=llm_instructions,
    )


def _setting(description: str, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "minimum": 0, "description": description}
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


#: The settings ``freeipa.pwpolicy.set`` can change, keyed like PWPOLICY_OPTIONS.
_PWPOLICY_SETTINGS: dict[str, dict[str, Any]] = {
    "max_life_days": _setting("Days a password lasts before the user must change it.", 20000),
    "min_life_hours": _setting("Hours before a user may change the password again.", 480000),
    "history_size": _setting("How many old passwords a user may not use again."),
    "min_classes": _setting(
        "How many kinds of characters a password needs: lower case, upper case, digits, special "
        "characters, other characters.",
        5,
    ),
    "min_length": _setting("The fewest characters a password may have."),
    "max_failures": _setting("Failed logins in a row before the account locks. 0: never locks."),
    "failure_reset_seconds": _setting(
        "Seconds after which the count of failed logins starts again at 0."
    ),
    "lockout_seconds": _setting("Seconds an account stays locked. 0: until an admin unlocks it."),
    "priority": _setting(
        "Only for a group's policy. When a user is in several groups with a policy, the policy "
        "with the lowest number counts."
    ),
}


ACCESS_WRITE_OPS: tuple[FreeipaTypedOp, ...] = (
    _create_op(
        op_id="freeipa.sudocmd.create",
        handler_attr="sudocmd_create",
        name_key="command",
        name_description="The full path of the program, for example /usr/bin/systemctl.",
        entry="The new sudo command; sudocmd holds its path.",
        example={"command": "/usr/bin/systemctl", "description": "Manage services"},
        tags=("sudocmd", "sudo-command"),
        summary=(
            "Create a sudo command in the IPA domain, so sudo rules can allow it. Waits until a "
            "second person approves."
        ),
        description=(
            "Runs the IPA command sudocmd_add. A sudo command is the full path of one program, "
            "for example /usr/bin/systemctl. Returns the new sudo command."
        ),
        llm_instructions={
            "when_to_call": (
                "Call before freeipa.sudorule.command.add: a sudo rule can only allow commands "
                "that exist as sudo commands. It adds a sudo command to the IPA domain."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} No sudo command with this path may exist. FreeIPA does not "
                "check that the program exists on any host."
            ),
            "output_shape": "{entry}: the new sudo command.",
            "next_step": (
                "To let a sudo rule run it, call freeipa.sudorule.command.add. A DuplicateEntry "
                "error means the sudo command exists already."
            ),
        },
    ),
    _create_op(
        op_id="freeipa.sudorule.create",
        handler_attr="sudorule_create",
        name_key="name",
        name_description="The new sudo rule's name.",
        entry="The new sudo rule, with FreeIPA's default attributes.",
        example={"name": "webadmins-systemctl", "description": "Web admins manage services"},
        tags=_SUDO_TAGS,
        summary=(
            "Create an empty sudo rule in the IPA domain. Waits until a second person approves."
        ),
        description=(
            "Runs the IPA command sudorule_add. The new rule is enabled but has no users, hosts "
            "or commands, so it allows nothing yet. Returns the new rule."
        ),
        llm_instructions={
            "when_to_call": (
                "Call to start a new sudo rule: who may run which commands with sudo, on which "
                "hosts. It adds a rule to the IPA domain."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} No sudo rule with this name may exist; check with "
                "freeipa.sudorule.list."
            ),
            "output_shape": "{entry}: the new rule; cn holds its name.",
            "next_step": (
                "Add users with freeipa.sudorule.user.add, hosts with freeipa.sudorule.host.add "
                "and commands with freeipa.sudorule.command.add. The commands run as root. A "
                "DuplicateEntry error means the rule exists already."
            ),
        },
    ),
    _member_add_op(
        op_id="freeipa.sudorule.user.add",
        handler_attr="sudorule_user_add",
        summary=(
            "Add users or user groups to a sudo rule: who may use it. Waits until a second person "
            "approves."
        ),
        description=(
            "Runs the IPA command sudorule_add_user. Fails and names each member FreeIPA did not "
            "add. A name FreeIPA does not know is kept as an external user and returned in "
            "external."
        ),
        name_description="The sudo rule's name.",
        entry="The sudo rule after the change, with its members.",
        members={"users": _USERS, "groups": _GROUPS},
        example={"name": "webadmins-systemctl", "groups": ["webadmins"]},
        tags=(*_SUDO_TAGS, "user", "membership"),
        external=True,
        llm_instructions={
            "when_to_call": "Call to say who may use a sudo rule. It changes the rule's users.",
            "preconditions": (
                f"{APPROVAL_NOTE} The rule must exist. Give users, groups or both. A rule that "
                "allows every user (user category all) takes no users."
            ),
            "output_shape": (
                "{entry, completed, external}: the rule after the change, how many names FreeIPA "
                "added, and the names it keeps as external users because it does not know them. "
                f"{_NOT_ADDED}"
            ),
            "next_step": (
                "Check external: a name there is not an IPA user or group, maybe a typo. Read "
                f"the rule with freeipa.sudorule.show. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    _member_add_op(
        op_id="freeipa.sudorule.host.add",
        handler_attr="sudorule_host_add",
        summary=(
            "Add hosts or host groups to a sudo rule: where it applies. Waits until a second "
            "person approves."
        ),
        description=(
            "Runs the IPA command sudorule_add_host. Fails and names each member FreeIPA did not "
            "add. A host name FreeIPA does not know is kept as an external host and returned in "
            "external."
        ),
        name_description="The sudo rule's name.",
        entry="The sudo rule after the change, with its members.",
        members={"hosts": _HOSTS, "hostgroups": _HOSTGROUPS},
        example={"name": "webadmins-systemctl", "hostgroups": ["webservers"]},
        tags=(*_SUDO_TAGS, "host", "membership"),
        external=True,
        llm_instructions={
            "when_to_call": (
                "Call to say on which hosts a sudo rule applies. It changes the rule's hosts."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The rule must exist. Give hosts, hostgroups or both. A rule "
                "that applies on every host (host category all) takes no hosts."
            ),
            "output_shape": (
                "{entry, completed, external}: the rule after the change, how many names FreeIPA "
                "added, and the host names it keeps as external hosts because it does not know "
                f"them. {_NOT_ADDED}"
            ),
            "next_step": (
                "Check external: a name there is not an IPA host, maybe a typo. Read the rule "
                f"with freeipa.sudorule.show. {UNTRUSTED_TEXT_NOTE}"
            ),
        },
    ),
    _member_add_op(
        op_id="freeipa.sudorule.command.add",
        handler_attr="sudorule_command_add",
        summary="Allow sudo commands in a sudo rule. Waits until a second person approves.",
        description=(
            "Runs the IPA command sudorule_add_allow_command with the paths of existing sudo "
            "commands. Fails and names each command FreeIPA did not add, for example one that is "
            "not a sudo command yet."
        ),
        name_description="The sudo rule's name.",
        entry="The sudo rule after the change, with its members.",
        members={
            "commands": names_schema(
                "Sudo commands to allow, by their full path. Each must exist as a sudo command.",
                "One sudo command's full path, for example /usr/bin/systemctl.",
            )
        },
        example={"name": "webadmins-systemctl", "commands": ["/usr/bin/systemctl"]},
        tags=(*_SUDO_TAGS, "sudocmd"),
        llm_instructions={
            "when_to_call": (
                "Call to say which commands a sudo rule lets its users run. It changes the rule's "
                "commands."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The rule and each sudo command must exist; make a missing one "
                "with freeipa.sudocmd.create. A rule that allows every command (command category "
                "all) takes no commands."
            ),
            "output_shape": (
                "{entry, completed}: the rule after the change, and how many commands FreeIPA "
                f"added. {_NOT_ADDED}"
            ),
            "next_step": f"Read the rule with freeipa.sudorule.show. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    _create_op(
        op_id="freeipa.hbacrule.create",
        handler_attr="hbacrule_create",
        name_key="name",
        name_description="The new access rule's name.",
        entry="The new access rule, with FreeIPA's default attributes.",
        example={"name": "webadmins-ssh", "description": "Web admins log in to web servers"},
        tags=_HBAC_TAGS,
        summary=(
            "Create an empty access rule (HBAC) in the IPA domain. Waits until a second person "
            "approves."
        ),
        description=(
            "Runs the IPA command hbacrule_add. The new rule is enabled but has no users, hosts "
            "or services, so it lets nobody in yet. Returns the new rule."
        ),
        llm_instructions={
            "when_to_call": (
                "Call to start a new access rule: which users may use which services on which "
                "hosts. It adds a rule to the IPA domain."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} No access rule with this name may exist; check with "
                "freeipa.hbacrule.list."
            ),
            "output_shape": "{entry}: the new rule; cn holds its name.",
            "next_step": (
                "Add users with freeipa.hbacrule.user.add, hosts with freeipa.hbacrule.host.add "
                "and services with freeipa.hbacrule.service.add. While the default rule allow_all "
                "is enabled, every user may still log in everywhere; freeipa.hbacrule.disable "
                "turns it off. A DuplicateEntry error means the rule exists already."
            ),
        },
    ),
    _member_add_op(
        op_id="freeipa.hbacrule.user.add",
        handler_attr="hbacrule_user_add",
        summary=(
            "Add users or user groups to an access rule (HBAC): who it lets in. Waits until a "
            "second person approves."
        ),
        description=(
            "Runs the IPA command hbacrule_add_user. Fails and names each member FreeIPA did not "
            "add, for example a user that does not exist."
        ),
        name_description="The access rule's name.",
        entry="The access rule after the change, with its members.",
        members={"users": _USERS, "groups": _GROUPS},
        example={"name": "webadmins-ssh", "groups": ["webadmins"]},
        tags=(*_HBAC_TAGS, "user", "membership"),
        llm_instructions={
            "when_to_call": "Call to say whom an access rule lets in. It changes the rule's users.",
            "preconditions": (
                f"{APPROVAL_NOTE} The rule and every member must exist. Give users, groups or "
                "both. A rule for every user (user category all, like allow_all) takes no users."
            ),
            "output_shape": (
                "{entry, completed}: the rule after the change, and how many members FreeIPA "
                f"added. {_NOT_ADDED}"
            ),
            "next_step": f"Test access with freeipa.hbac.test. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    _member_add_op(
        op_id="freeipa.hbacrule.host.add",
        handler_attr="hbacrule_host_add",
        summary=(
            "Add hosts or host groups to an access rule (HBAC): where it lets users in. Waits for "
            "approval."
        ),
        description=(
            "Runs the IPA command hbacrule_add_host. Fails and names each member FreeIPA did not "
            "add, for example a host that does not exist."
        ),
        name_description="The access rule's name.",
        entry="The access rule after the change, with its members.",
        members={"hosts": _HOSTS, "hostgroups": _HOSTGROUPS},
        example={"name": "webadmins-ssh", "hostgroups": ["webservers"]},
        tags=(*_HBAC_TAGS, "host", "membership"),
        llm_instructions={
            "when_to_call": (
                "Call to say on which hosts an access rule lets users in. It changes the rule's "
                "hosts."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The rule and every member must exist. Give hosts, hostgroups or "
                "both. A rule for every host (host category all) takes no hosts."
            ),
            "output_shape": (
                "{entry, completed}: the rule after the change, and how many members FreeIPA "
                f"added. {_NOT_ADDED}"
            ),
            "next_step": f"Test access with freeipa.hbac.test. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    _member_add_op(
        op_id="freeipa.hbacrule.service.add",
        handler_attr="hbacrule_service_add",
        summary=(
            "Add services, for example sshd, to an access rule (HBAC): what it lets users use. "
            "Waits for approval."
        ),
        description=(
            "Runs the IPA command hbacrule_add_service with HBAC services and HBAC service "
            "groups. Fails and names each member FreeIPA did not add."
        ),
        name_description="The access rule's name.",
        entry="The access rule after the change, with its members.",
        members={
            "services": names_schema(
                "HBAC services to add, by name, for example sshd or sudo.",
                "One HBAC service's name.",
            ),
            "service_groups": names_schema(
                "HBAC service groups to add, by name, for example Sudo.",
                "One HBAC service group's name.",
            ),
        },
        example={"name": "webadmins-ssh", "services": ["sshd"]},
        tags=(*_HBAC_TAGS, "service", "membership"),
        llm_instructions={
            "when_to_call": (
                "Call to say which services an access rule lets its users use, for example sshd "
                "to log in. It changes the rule's services."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The rule and every member must exist. Give services, "
                "service_groups or both. A rule for every service (service category all) takes no "
                "services."
            ),
            "output_shape": (
                "{entry, completed}: the rule after the change, and how many members FreeIPA "
                f"added. {_NOT_ADDED}"
            ),
            "next_step": f"Test access with freeipa.hbac.test. {UNTRUSTED_TEXT_NOTE}",
        },
    ),
    write_op(
        op_id=HBACRULE_DISABLE_OP_ID,
        handler_attr="hbacrule_disable",
        summary=(
            "Disable an access rule (HBAC), also allow_all. Waits for approval; for allow_all the "
            "approval lists the other rules."
        ),
        description=(
            "Runs the IPA command hbacrule_disable. The rule stays, but lets nobody in until it is "
            "enabled again. For allow_all, the approval preview lists the other enabled access "
            "rules."
        ),
        group_key=GROUP_ACCESS,
        properties={"name": name_schema("The access rule's name, for example allow_all.")},
        required=["name"],
        example={"name": "allow_all"},
        response={
            "disabled": {"type": "string", "description": "The name of the rule now disabled."}
        },
        tags=_HBAC_TAGS,
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to stop an access rule from letting users in, for example the default rule "
                "allow_all once your own rules are in place. It changes whether the rule is "
                "enabled."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} The approval names the rule; for allow_all it also lists the "
                "other enabled access rules. Before you disable allow_all, check that the other "
                "rules let in every user who needs access: call freeipa.hbac.test with rules set "
                "to those rules."
            ),
            "output_shape": (
                "{disabled}: the rule's name. A rule that was disabled already also gives this "
                "answer."
            ),
            "next_step": (
                "Test access with freeipa.hbac.test. A NotFound error means no such rule exists."
            ),
        },
    ),
    write_op(
        op_id=PWPOLICY_SET_OP_ID,
        handler_attr="pwpolicy_set",
        summary=(
            "Change the password policy of a user group, or the global policy. Waits until a "
            "second person approves."
        ),
        description=(
            "Runs the IPA command pwpolicy_mod. Without group, it changes the global policy, which "
            "applies to every user whose groups have no policy of their own. Changes only the "
            "values given."
        ),
        group_key=GROUP_IDENTITY,
        properties={
            "group": name_schema(
                "The user group whose policy to change. Leave it out for the global policy. The "
                "group must have a policy already."
            ),
            **_PWPOLICY_SETTINGS,
        },
        required=[],
        any_of=[{"required": [key]} for key in PWPOLICY_OPTIONS],
        dependent_required={"priority": ["group"]},
        example={"group": "admins", "max_life_days": 90, "min_length": 12},
        response={
            "entry": entry_schema(
                "The password policy after the change, with FreeIPA's attributes."
            )
        },
        # No "password" tag: MEHO would then treat the op as one that sends a
        # secret, and hide FreeIPA's error text from the caller.
        tags=("pwpolicy", "policy"),
        safety_level="caution",
        llm_instructions={
            "when_to_call": (
                "Call to change the password rules of a user group, or the global rules: how "
                "long a password lasts, how long it must be, and when an account locks."
            ),
            "preconditions": (
                f"{APPROVAL_NOTE} Give at least one value; read the policy first with "
                "freeipa.pwpolicy.show. Without group, the change is for the global policy, which "
                "applies to every user whose groups have no policy of their own; the approval "
                "says so. priority needs group."
            ),
            "output_shape": (
                "{entry}: the policy after the change, with FreeIPA's attribute names, for "
                "example krbmaxpwdlife (days) and krbpwdminlength."
            ),
            "next_step": (
                "An EmptyModlist error means the policy has these values already. A NotFound "
                "error means the group has no policy of its own."
            ),
        },
    ),
)
