# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registration metadata and the flow shape of ``keycloak.authflow.ensure_approval_touch``.

The handler is in :mod:`.ops_authflow_touch`. The flow shape here is the one
of the chart's realm file (``deploy/charts/meho/charts/keycloak/files/
meho-realm.json``); a test keeps the two the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meho_backplane.connectors.keycloak.ops_read import KeycloakOp

__all__ = [
    "AMR_MAPPER",
    "ENSURE_APPROVAL_TOUCH_OP_ID",
    "LOA_MAP_ATTRIBUTE",
    "REDIRECTOR",
    "REDIRECT_CONFIG_ALIAS",
    "TOUCH_ACR",
    "TOUCH_FLOW_ALIAS",
    "TOUCH_FLOW_STEPS",
    "TOUCH_LEVEL",
    "TOUCH_OPS",
    "FlowNode",
]

ENSURE_APPROVAL_TOUCH_OP_ID = "keycloak.authflow.ensure_approval_touch"
TOUCH_FLOW_ALIAS = "meho approval touch"
#: The name MEHO asks for in ``acr_values``, and its level.
TOUCH_ACR = "meho-touch"
TOUCH_LEVEL = 3
#: The realm attribute that maps ``acr`` names to levels.
LOA_MAP_ATTRIBUTE = "acr.loa.map"
#: The step that sends people straight to the company login, and the name of
#: its settings on the touch flow (the same name core/company_login.py uses).
REDIRECTOR = "identity-provider-redirector"
REDIRECT_CONFIG_ALIAS = "meho-company-login-redirect-touch"
TOUCH_FLOW_DESCRIPTION = (
    "Console login of meho-web. Level 1 is the normal login. Level 3 (acr "
    "meho-touch) asks for a passkey touch every time; MEHO asks for it before a "
    "dangerous or destructive approval."
)

#: The mapper that writes the passkey step's reference value into ``amr``.
AMR_MAPPER: dict[str, Any] = {
    "name": "amr",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-amr-mapper",
    "config": {
        "id.token.claim": "true",
        "access.token.claim": "true",
        "introspection.token.claim": "true",
    },
}


@dataclass(frozen=True)
class FlowNode:
    """One step of the flow, or (with ``children``) one sub-flow.

    ``name`` is the step's provider, or the sub-flow's alias. ``config`` is
    the alias and the settings of a step that has settings.
    """

    name: str
    requirement: str
    config: tuple[str, dict[str, str]] | None = None
    children: tuple[FlowNode, ...] | None = None
    description: str = ""


_T = TOUCH_FLOW_ALIAS

#: The steps of the flow, in order. Level 1 has the steps of Keycloak's own
#: browser flow; level 3 is the passkey touch.
TOUCH_FLOW_STEPS: tuple[FlowNode, ...] = (
    FlowNode("auth-cookie", "ALTERNATIVE"),
    FlowNode(
        f"{_T} - levels",
        "ALTERNATIVE",
        description="Level 1, then level 3 when it is asked for.",
        children=(
            FlowNode(
                f"{_T} - level 1",
                "CONDITIONAL",
                description=(
                    "The normal login: company login or password, then the one-time "
                    "code when the person has one."
                ),
                children=(
                    FlowNode(
                        "conditional-level-of-authentication",
                        "REQUIRED",
                        config=(
                            f"{_T} - level 1",
                            {"loa-condition-level": "1", "loa-max-age": "36000"},
                        ),
                    ),
                    FlowNode(
                        f"{_T} - sign-in",
                        "REQUIRED",
                        description="Company login, or the password form.",
                        children=(
                            FlowNode(REDIRECTOR, "ALTERNATIVE"),
                            FlowNode(
                                f"{_T} - password",
                                "ALTERNATIVE",
                                description=(
                                    "The password form (it also takes a passkey), then "
                                    "the one-time code."
                                ),
                                children=(
                                    FlowNode("auth-username-password-form", "REQUIRED"),
                                    FlowNode(
                                        f"{_T} - one-time code",
                                        "CONDITIONAL",
                                        description=(
                                            "Asks for the one-time code when the person "
                                            "has set one up and did not use a passkey."
                                        ),
                                        children=(
                                            FlowNode("conditional-user-configured", "REQUIRED"),
                                            FlowNode(
                                                "conditional-credential",
                                                "REQUIRED",
                                                config=(
                                                    f"{_T} - skip code after passkey",
                                                    {
                                                        "credentials": "webauthn-passwordless",
                                                        "included": "false",
                                                    },
                                                ),
                                            ),
                                            FlowNode("auth-otp-form", "REQUIRED"),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            FlowNode(
                f"{_T} - level 3",
                "CONDITIONAL",
                description="The passkey touch: a passkey with user verification, every time.",
                children=(
                    FlowNode(
                        "conditional-level-of-authentication",
                        "REQUIRED",
                        config=(
                            f"{_T} - level 3",
                            {"loa-condition-level": "3", "loa-max-age": "0"},
                        ),
                    ),
                    FlowNode(
                        "webauthn-authenticator-passwordless",
                        "REQUIRED",
                        config=(
                            f"{_T} - passkey",
                            {"default.reference.value": "hwk", "default.reference.maxAge": "120"},
                        ),
                    ),
                ),
            ),
        ),
    ),
)

_WHEN_TO_USE = (
    "Use to give a running realm the console login flow with the passkey "
    "touch level ('meho approval touch'). Keycloak reads the chart's realm "
    "file only when the realm is new. The op makes the flow, maps the acr "
    "name 'meho-touch' to level 3 in the realm, adds the 'amr' mapper to the "
    "console client, and binds the flow as the console client's browser flow. "
    "It binds only when the realm uses Keycloak's built-in login and the client "
    "has no login flow of its own, so the normal console login stays the same; "
    "only a login that asks for 'meho-touch' needs a passkey. Safe to run twice. "
    "A flow with that name "
    "but another shape is not changed: the op stops and names the difference. "
    "It needs approval."
)

_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "The console client. Default 'meho-web'.",
        },
        "replace_custom_login": {
            "type": "boolean",
            "default": False,
            "description": (
                "Bind the flow also when the realm or the client has its own login "
                "flow. That login's own steps then no longer apply to the console."
            ),
        },
    },
    "additionalProperties": False,
    "examples": [{}, {"client_id": "meho-web"}, {"replace_custom_login": True}],
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The target's managed realm."},
        "flow_alias": {"type": "string", "description": "Always 'meho approval touch'."},
        "flow_id": {"type": "string", "description": "Internal id of the flow."},
        "flow_created": {
            "type": "boolean",
            "description": "true: this run made the flow. false: it was already there.",
        },
        "flow_steps": {
            "type": "array",
            "description": "The flow's steps as read back from Keycloak.",
            "items": {
                "type": "object",
                "properties": {
                    "level": {"type": ["integer", "null"]},
                    "step": {"type": "string"},
                    "requirement": {"type": ["string", "null"]},
                },
            },
        },
        "loa_map_changed": {
            "type": "boolean",
            "description": "true: this run mapped 'meho-touch' to level 3 in the realm.",
        },
        "amr_mapper_added": {
            "type": "boolean",
            "description": "true: this run added the 'amr' mapper to the client.",
        },
        "amr_mapper_fixed": {
            "type": "boolean",
            "description": (
                "true: the client had an 'amr' mapper that did not write the claim "
                "into every token; this run turned those token settings on."
            ),
        },
        "redirect_default_copied": {
            "type": "boolean",
            "description": (
                "true: this run gave the flow the company login default of the "
                "realm's browser flow, so the console still goes straight to it."
            ),
        },
        "client_id": {"type": "string", "description": "The client the flow is bound to."},
        "client_uuid": {"type": "string", "description": "Internal id of the client."},
        "bound": {
            "type": "boolean",
            "description": "true: the flow is the client's browser flow after the run.",
        },
        "not_bound_reason": {
            "type": ["string", "null"],
            "description": (
                "Why the flow was not bound: the realm or the client has its own "
                "login flow. null when it was bound."
            ),
        },
        "browser_flow_id": {
            "type": ["string", "null"],
            "description": "The client's browser flow after the run; null when it has none.",
        },
        "previous_browser_flow_id": {
            "type": ["string", "null"],
            "description": "The client's browser flow before the run; null when it had none.",
        },
        "binding_changed": {
            "type": "boolean",
            "description": "true: this run bound the flow. false: it was already bound.",
        },
    },
    "required": [
        "realm",
        "flow_alias",
        "flow_id",
        "flow_created",
        "flow_steps",
        "loa_map_changed",
        "amr_mapper_added",
        "amr_mapper_fixed",
        "redirect_default_copied",
        "client_id",
        "client_uuid",
        "bound",
        "not_bound_reason",
        "browser_flow_id",
        "previous_browser_flow_id",
        "binding_changed",
    ],
}

TOUCH_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id=ENSURE_APPROVAL_TOUCH_OP_ID,
        handler_attr="authflow_ensure_approval_touch",
        summary="Give the console client the login flow with the passkey touch level.",
        description=(
            "Makes the login flow 'meho approval touch', maps 'meho-touch' to "
            "level 3, adds the 'amr' mapper to the console client, and binds the "
            "flow as its browser flow. Changes nothing when all is there; stops "
            "when the flow has another shape."
        ),
        parameter_schema=_PARAMETER_SCHEMA,
        response_schema=_RESPONSE_SCHEMA,
        group_key="authflow_write",
        tags=("login-flow", "passkey", "client", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE,
            "when_to_call": (
                "When a realm made before the touch flow existed must get it, so "
                "that MEHO can ask for a passkey touch before an approval. Also to "
                "check it: a second run changes nothing."
            ),
            "preconditions": (
                "The console client (default 'meho-web') must exist in the "
                "target's managed realm. The realm's passkey policy must ask for "
                "user verification ('required'). People who approve need a passkey."
            ),
            "parameter_hints": {
                "client_id": "Leave it out to use 'meho-web'.",
                "replace_custom_login": (
                    "Leave it out. Set it to true only after a person checked that "
                    "the console may lose the steps of the realm's or the client's "
                    "own login flow."
                ),
            },
            "output_shape": (
                "``{realm, flow_alias, flow_id, flow_created, flow_steps: "
                "[{level, step, requirement}], loa_map_changed, amr_mapper_added, "
                "amr_mapper_fixed, redirect_default_copied, client_id, client_uuid, "
                "bound, not_bound_reason, browser_flow_id, previous_browser_flow_id, "
                "binding_changed}``."
            ),
            "next_step": (
                "All six change flags false: the realm already had everything. An "
                "error with 'other_steps' names the difference; a person fixes the "
                "flow in the Keycloak admin console, or deletes it and runs again. "
                "An error with 'loa_conflict' means the realm maps 'meho-touch' to "
                "another level. An error with 'verification_not_required' means the "
                "realm's passkey policy does not ask for a PIN or fingerprint; a person "
                "sets it to 'required' and runs again. bound false: the realm or the client "
                "has its own login flow, or 'browser with passkeys' has other steps than "
                "the built-in login with the passkey step; not_bound_reason says which. "
                "The flow is made but not used; a person decides, and runs again with "
                "replace_custom_login true to bind it anyway. The way back: "
                "keycloak.client.update with "
                '{"authenticationFlowBindingOverrides": {"browser": ""}}.'
            ),
        },
    ),
)
