# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry rows of the host local-user and Active Directory changes.

``_register.py`` collects :data:`COMPOSITES`, and ``_governed_subops.py`` reads
:data:`HOST_ACCESS_GOVERNED_SUBOPS`. Every change has
``requires_approval=True``. They share the group of the two typed reads
(``host_access.HOST_ACCESS_GROUP_KEY``), with the same text.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _host_ad, _host_local_users
from meho_backplane.connectors.vmware_rest.composites._host_access_schemas import (
    AD_JOIN_LLM_INSTRUCTIONS,
    AD_JOIN_PARAMETER_SCHEMA,
    AD_JOIN_RESPONSE_SCHEMA,
    AD_LEAVE_LLM_INSTRUCTIONS,
    AD_LEAVE_PARAMETER_SCHEMA,
    AD_LEAVE_RESPONSE_SCHEMA,
    LOCAL_USER_CREATE_LLM_INSTRUCTIONS,
    LOCAL_USER_CREATE_PARAMETER_SCHEMA,
    LOCAL_USER_CREATE_RESPONSE_SCHEMA,
    LOCAL_USER_DELETE_LLM_INSTRUCTIONS,
    LOCAL_USER_DELETE_PARAMETER_SCHEMA,
    LOCAL_USER_DELETE_RESPONSE_SCHEMA,
    LOCAL_USER_PASSWORD_SET_LLM_INSTRUCTIONS,
    LOCAL_USER_PASSWORD_SET_PARAMETER_SCHEMA,
    LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.composites._spec import CompositeSpec
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_LIST_HOSTS,
    _OP_RETRIEVE_PROPERTIES,
)
from meho_backplane.connectors.vmware_rest.host_access import (
    HOST_ACCESS_GROUP_KEY,
    OP_CREATE_USER,
    OP_JOIN_DOMAIN,
    OP_LEAVE_DOMAIN,
    OP_REMOVE_USER,
    OP_RETRIEVE_ACCESS_ENTRIES,
    OP_RETRIEVE_SERVICE_CONTENT,
    OP_RETRIEVE_USER_GROUPS,
    OP_UPDATE_USER,
)

__all__ = ["COMPOSITES", "HOST_ACCESS_GOVERNED_SUBOPS"]

_USER_TAGS: Final = ("composite", "write", "host", "esxi", "users", "security")
_AD_TAGS: Final = ("composite", "write", "host", "active-directory", "security", "vi-json")

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id=_host_local_users.OP_ID_LOCAL_USER_CREATE,
        handler=_host_local_users.local_user_create_composite,
        summary="Create a local user on one ESXi host; the password comes from Vault.",
        description=(
            "Creates a local user on an ESXi host, with the password read from Vault. Needs "
            "the ESXi host itself as the target. Sends nothing when the user exists; reads "
            "the users back after."
        ),
        parameter_schema=LOCAL_USER_CREATE_PARAMETER_SCHEMA,
        response_schema=LOCAL_USER_CREATE_RESPONSE_SCHEMA,
        group_key=HOST_ACCESS_GROUP_KEY,
        tags=list(_USER_TAGS),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=LOCAL_USER_CREATE_LLM_INSTRUCTIONS,
    ),
    CompositeSpec(
        op_id=_host_local_users.OP_ID_LOCAL_USER_PASSWORD_SET,
        handler=_host_local_users.local_user_password_set_composite,
        summary="Set the password of a local user on one ESXi host, from Vault.",
        description=(
            "Sets a new password for a local user of an ESXi host, read from Vault. Needs the "
            "ESXi host itself as the target. Sends nothing when the user does not exist; "
            "reads the user back after."
        ),
        parameter_schema=LOCAL_USER_PASSWORD_SET_PARAMETER_SCHEMA,
        response_schema=LOCAL_USER_PASSWORD_SET_RESPONSE_SCHEMA,
        group_key=HOST_ACCESS_GROUP_KEY,
        tags=list(_USER_TAGS),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=LOCAL_USER_PASSWORD_SET_LLM_INSTRUCTIONS,
    ),
    CompositeSpec(
        op_id=_host_local_users.OP_ID_LOCAL_USER_DELETE,
        handler=_host_local_users.local_user_delete_composite,
        summary="Delete a local user from one ESXi host.",
        description=(
            "Deletes a local user and its access role from an ESXi host. Needs the ESXi host "
            "itself as the target and a preview first; never deletes root. Reads the users "
            "back after."
        ),
        parameter_schema=LOCAL_USER_DELETE_PARAMETER_SCHEMA,
        response_schema=LOCAL_USER_DELETE_RESPONSE_SCHEMA,
        group_key=HOST_ACCESS_GROUP_KEY,
        tags=list(_USER_TAGS),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=LOCAL_USER_DELETE_LLM_INSTRUCTIONS,
    ),
    CompositeSpec(
        op_id=_host_ad.OP_ID_AD_JOIN,
        handler=_host_ad.ad_join_composite,
        summary="Join one ESXi host to an Active Directory domain; the password is in Vault.",
        description=(
            "Joins a host to an Active Directory domain with a join account whose password is "
            "read from Vault. Works through vCenter or on the host. Sends nothing when the "
            "host already is in the domain; reads it back after."
        ),
        parameter_schema=AD_JOIN_PARAMETER_SCHEMA,
        response_schema=AD_JOIN_RESPONSE_SCHEMA,
        group_key=HOST_ACCESS_GROUP_KEY,
        tags=list(_AD_TAGS),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=AD_JOIN_LLM_INSTRUCTIONS,
    ),
    CompositeSpec(
        op_id=_host_ad.OP_ID_AD_LEAVE,
        handler=_host_ad.ad_leave_composite,
        summary="Take one ESXi host out of its Active Directory domain.",
        description=(
            "Takes a host out of its Active Directory domain; force also deletes the "
            "permissions of domain users. Works through vCenter or on the host. Sends nothing "
            "when the host is in no domain; reads it back after."
        ),
        parameter_schema=AD_LEAVE_PARAMETER_SCHEMA,
        response_schema=AD_LEAVE_RESPONSE_SCHEMA,
        group_key=HOST_ACCESS_GROUP_KEY,
        tags=list(_AD_TAGS),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=AD_LEAVE_LLM_INSTRUCTIONS,
        # ``.leave`` is not a classifier suffix. The user create, the password
        # change and the join send a password: they are credential_write.
        classifier_other=True,
    ),
)

#: The reads of the local-user changes (on the ESXi host).
_USER_READS: Final = (OP_RETRIEVE_SERVICE_CONTENT, OP_RETRIEVE_USER_GROUPS)
#: The reads of the domain changes (the host list only on a vCenter target).
_AD_READS: Final = (_OP_LIST_HOSTS, _OP_RETRIEVE_PROPERTIES)

#: composite op id -> the vendor calls it may make (``_VIM_SUB_OPS_*`` style).
HOST_ACCESS_GOVERNED_SUBOPS: Final[dict[str, tuple[str, ...]]] = {
    _host_local_users.OP_ID_LOCAL_USER_CREATE: (*_USER_READS, OP_CREATE_USER),
    _host_local_users.OP_ID_LOCAL_USER_PASSWORD_SET: (*_USER_READS, OP_UPDATE_USER),
    _host_local_users.OP_ID_LOCAL_USER_DELETE: (
        *_USER_READS,
        _OP_RETRIEVE_PROPERTIES,
        OP_RETRIEVE_ACCESS_ENTRIES,
        OP_REMOVE_USER,
    ),
    _host_ad.OP_ID_AD_JOIN: (*_AD_READS, OP_JOIN_DOMAIN),
    _host_ad.OP_ID_AD_LEAVE: (*_AD_READS, OP_LEAVE_DOMAIN),
}
