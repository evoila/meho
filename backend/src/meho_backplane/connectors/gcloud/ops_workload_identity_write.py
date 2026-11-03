# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The operations of ``gcloud-rest-1.0`` that set up keyless Google access.

An outside workload (for example a pod in a private Kubernetes cluster)
gets short-lived Google access without a key file in three steps. Each step
has an operation here, and each one waits for a person to approve it:

* ``gcloud.iam.workload_identity_pools.create`` -- make a pool (caution).
* ``gcloud.iam.workload_identity_pools.providers.create`` -- make an OIDC
  provider in the pool: which issuer is trusted, optionally with uploaded
  public keys and a condition (dangerous).
* ``gcloud.iam.workload_identity_pools.providers.update_keys`` -- replace
  the uploaded public keys, for example after the cluster got a new signing
  key (dangerous).
* ``gcloud.iam.service_accounts.binding.add`` -- add one member (usually a
  ``principal://`` or ``principalSet://`` of the pool) to one role
  (usually ``roles/iam.workloadIdentityUser``) on a service account
  (dangerous).

The handlers live in :mod:`meho_backplane.connectors.gcloud.workload_identity_write`.

Google REST references:

* https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools/create
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers/create
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.locations.workloadIdentityPools.providers/patch
* https://cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/setIamPolicy
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.gcloud.ops import GcloudOp
from meho_backplane.connectors.gcloud.ops_workload_identity import (
    _POOL_ID_PROP,
    _SA_PROP,
    GROUP_WORKLOAD_IDENTITY,
)
from meho_backplane.connectors.gcloud.workload_identity import MEMBER_PATTERN, ROLE_PATTERN
from meho_backplane.connectors.gcloud.workload_identity_write import (
    ISSUER_URI_PATTERN,
    JWK_TYPES,
    PROVIDER_ID_PATTERN,
)

__all__ = ["WORKLOAD_IDENTITY_WRITE_OPS"]

_TAGS: Final[tuple[str, ...]] = ("gcloud", "iam", "write")

_EXAMPLE_SA: Final[str] = "index-reader@example-project.iam.gserviceaccount.com"
_EXAMPLE_MEMBER: Final[str] = (
    "principalSet://iam.googleapis.com/projects/123456789012/locations/global/"
    "workloadIdentityPools/cluster-pool/attribute.namespace/search"
)
_EXAMPLE_JWKS: Final[dict[str, Any]] = {
    "keys": [
        {
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "kid": "example-key-1",
            "n": "example-rsa-modulus-base64url",
            "e": "AQAB",
        }
    ]
}

_PROVIDER_ID_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": PROVIDER_ID_PATTERN,
    "description": (
        "The provider ID: 4 to 32 characters, a-z, 0-9 and '-'; may not start with 'gcp-'."
    ),
}

_JWK_FIELD_TEXT: Final[dict[str, str]] = {
    "kty": "The key type: RSA or EC.",
    "alg": "The signing algorithm, e.g. RS256 or ES256.",
    "use": "What the key is for, usually 'sig'.",
    "kid": "The key ID the tokens name in their header.",
    "n": "RSA: the modulus (base64url).",
    "e": "RSA: the public exponent (base64url), usually AQAB.",
    "x": "EC: the x coordinate (base64url).",
    "y": "EC: the y coordinate (base64url).",
    "crv": "EC: the curve, e.g. P-256.",
}

_JWKS_PROP: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "The public keys that sign the outside tokens, as a key set {keys: [...]}. For a "
        "Kubernetes cluster this is the answer of /openid/v1/jwks. Public keys only: a key "
        "with private parts (d, p, q, ...) is refused. Google accepts RSA and EC keys and "
        "only the fields listed here."
    ),
    "properties": {
        "keys": {
            "type": "array",
            "minItems": 1,
            "description": "One entry per public key.",
            "items": {
                "type": "object",
                "properties": {
                    name: (
                        {"type": "string", "enum": list(JWK_TYPES), "description": text}
                        if name == "kty"
                        else {"type": "string", "description": text}
                    )
                    for name, text in _JWK_FIELD_TEXT.items()
                },
                "required": ["kty"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["keys"],
    "additionalProperties": False,
}

_DISPLAY_NAME_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "maxLength": 32,
    "description": "Optional short name shown in the Google console (at most 32 characters).",
}

_DESCRIPTION_PROP: Final[dict[str, Any]] = {
    "type": "string",
    "maxLength": 256,
    "description": "Optional text that says what this is for (at most 256 characters).",
}

_OPERATION_PROPS: Final[dict[str, Any]] = {
    "operation_name": {
        "type": ["string", "null"],
        "description": "The Google operation that did the change; null when nothing was sent.",
    },
    "operation_done": {
        "type": ["boolean", "null"],
        "description": (
            "True when Google reported the operation as finished; false when it was still "
            "running after a short wait; null when nothing was sent."
        ),
    },
    "note": {"type": "string", "description": "What the result means, in plain words."},
}

_POOL_CREATE_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.create",
    handler_attr="gcloud_wif_pool_create",
    summary="Create one workload identity pool, so an outside system can get keyless access.",
    description=(
        "Sends POST .../locations/global/workloadIdentityPools?workloadIdentityPoolId=<id> "
        "and waits a short time for Google's operation. The result shows the pool state "
        "(ACTIVE). An ID that is already used changes nothing. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "pool_id": {
                **_POOL_ID_PROP,
                "description": (
                    "The new pool ID: 4 to 32 characters, a-z, 0-9 and '-'; may not start "
                    "with 'gcp-'."
                ),
            },
            "display_name": _DISPLAY_NAME_PROP,
            "description": _DESCRIPTION_PROP,
        },
        "required": ["pool_id"],
        "additionalProperties": False,
        "examples": [{"pool_id": "cluster-pool", "display_name": "Search cluster"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "pool_id": {"type": "string", "description": "The pool ID."},
            "name": {"type": "string", "description": "The full resource name."},
            "state": {
                "type": "string",
                "description": (
                    "ACTIVE when the pool is ready; CREATE_PENDING when Google's operation was "
                    "still running after a short wait."
                ),
            },
            "already_existed": {
                "type": "boolean",
                "description": "True when a pool with this ID was already there; nothing changed.",
            },
            "display_name": {"type": ["string", "null"], "description": "The short name."},
            "description": {"type": ["string", "null"], "description": "The text."},
            **_OPERATION_PROPS,
        },
        "required": ["pool_id", "name", "state", "already_existed", "note"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "create", "workload-identity"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call as the first step when an outside workload (for example a pod in a "
            "Kubernetes cluster) needs Google access without a key file. A pool alone grants "
            "nothing; it holds the providers."
        ),
        "preconditions": (
            "A person must approve the call. The login needs iam.workloadIdentityPools.create "
            "on the project; MEHO checks this before it asks for approval. A deleted pool "
            "keeps its ID for 30 days, so that ID cannot be used again in that time."
        ),
        "output_shape": (
            "{pool_id, name, state, already_existed, display_name, description, "
            "operation_name, operation_done, note}. state is ACTIVE when ready, or "
            "CREATE_PENDING when Google was still working after a short wait."
        ),
        "next_step": (
            "Create the OIDC provider with gcloud.iam.workload_identity_pools.providers.create."
        ),
    },
)

_PROVIDER_CREATE_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.providers.create",
    handler_attr="gcloud_wif_provider_create",
    summary="Create an OIDC provider in a pool: which issuer, public keys and condition to trust.",
    description=(
        "Sends POST .../workloadIdentityPools/<pool>/providers?workloadIdentityPoolProviderId="
        "<id> with oidc.issuerUri, optional uploaded public keys (oidc.jwksJson), audiences, "
        "attributeMapping and attributeCondition, then waits for Google. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "pool_id": _POOL_ID_PROP,
            "provider_id": _PROVIDER_ID_PROP,
            "issuer_uri": {
                "type": "string",
                "pattern": ISSUER_URI_PATTERN,
                "description": (
                    "The issuer URL of the outside tokens (their 'iss' claim). Google accepts "
                    "only https://."
                ),
            },
            "jwks": _JWKS_PROP,
            "allowed_audiences": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "One accepted 'aud' value.",
                },
                "description": (
                    "Optional accepted values of the token's 'aud' claim (at most 10). When "
                    "empty, the token's audience must be the provider's full resource name."
                ),
            },
            "attribute_mapping": {
                "type": "object",
                "additionalProperties": {"type": "string", "maxLength": 2048},
                "required": ["google.subject"],
                "description": (
                    "How token claims map to Google attributes; must include google.subject. "
                    'Default {"google.subject": "assertion.sub"}.'
                ),
            },
            "attribute_condition": {
                "type": "string",
                "maxLength": 4096,
                "description": (
                    "Optional condition (a CEL expression) a token must meet, e.g. "
                    "\"assertion.sub == 'system:serviceaccount:search:reader'\". Without it, "
                    "every valid token of the issuer is accepted."
                ),
            },
            "display_name": _DISPLAY_NAME_PROP,
            "description": _DESCRIPTION_PROP,
        },
        "required": ["pool_id", "provider_id", "issuer_uri"],
        "additionalProperties": False,
        "examples": [
            {
                "pool_id": "cluster-pool",
                "provider_id": "cluster-oidc",
                "issuer_uri": "https://kubernetes.default.svc.cluster.local",
                "jwks": _EXAMPLE_JWKS,
                "attribute_mapping": {
                    "google.subject": "assertion.sub",
                    "attribute.namespace": "assertion['kubernetes.io']['namespace']",
                },
                "attribute_condition": "attribute.namespace == 'search'",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "pool_id": {"type": "string", "description": "The pool ID."},
            "provider_id": {"type": "string", "description": "The provider ID."},
            "name": {"type": "string", "description": "The full resource name."},
            "state": {
                "type": ["string", "null"],
                "description": (
                    "ACTIVE when the provider is ready; CREATE_PENDING when Google's operation "
                    "was still running after a short wait."
                ),
            },
            "already_existed": {
                "type": "boolean",
                "description": (
                    "True when a provider with this ID was already there; nothing changed and "
                    "the fields show that provider."
                ),
            },
            "issuer_uri": {"type": ["string", "null"], "description": "The trusted issuer."},
            "key_count": {
                "type": "integer",
                "description": (
                    "How many public keys are uploaded; 0 means Google reads them from the issuer."
                ),
            },
            "attribute_condition": {
                "type": ["string", "null"],
                "description": "The condition a token must meet; null means none.",
            },
            **_OPERATION_PROPS,
        },
        "required": ["pool_id", "provider_id", "name", "already_existed", "key_count", "note"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "create", "workload-identity"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after the pool exists, to say which outside tokens Google trusts. Upload the "
            "public keys (jwks) when Google cannot reach the issuer, for example a private "
            "Kubernetes cluster; give a condition so only the intended workload is accepted."
        ),
        "preconditions": (
            "A person must approve the call. The pool must exist, and the login needs "
            "iam.workloadIdentityPoolProviders.create on the project; MEHO checks both before "
            "it asks for approval. jwks holds public keys only: RSA or EC, no private parts."
        ),
        "output_shape": (
            "{pool_id, provider_id, name, state, already_existed, issuer_uri, key_count, "
            "attribute_condition, operation_name, operation_done, note}. Key values are "
            "never returned."
        ),
        "next_step": (
            "Let the outside identity act as a service account with "
            "gcloud.iam.service_accounts.binding.add and roles/iam.workloadIdentityUser."
        ),
    },
)

_UPDATE_KEYS_OP = GcloudOp(
    op_id="gcloud.iam.workload_identity_pools.providers.update_keys",
    handler_attr="gcloud_wif_provider_update_keys",
    summary="Replace the uploaded public keys of one OIDC provider.",
    description=(
        "Reads the provider, then sends PATCH .../providers/<id>?updateMask=oidc.jwksJson with "
        "the new key set and waits for Google. The same keys change nothing. Needed when the "
        "outside issuer gets a new signing key. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "pool_id": _POOL_ID_PROP,
            "provider_id": _PROVIDER_ID_PROP,
            "jwks": {
                **_JWKS_PROP,
                "description": (
                    "The full new key set {keys: [...]}; it replaces every uploaded key. Keep "
                    "the old key in the set while tokens signed with it are still in use. "
                    "Public keys only (RSA or EC)."
                ),
            },
        },
        "required": ["pool_id", "provider_id", "jwks"],
        "additionalProperties": False,
        "examples": [
            {"pool_id": "cluster-pool", "provider_id": "cluster-oidc", "jwks": _EXAMPLE_JWKS}
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "pool_id": {"type": "string", "description": "The pool ID."},
            "provider_id": {"type": "string", "description": "The provider ID."},
            "name": {"type": "string", "description": "The full resource name."},
            "issuer_uri": {"type": ["string", "null"], "description": "The trusted issuer."},
            "keys_before": {
                "type": "integer",
                "description": "How many keys were uploaded before the call.",
            },
            "key_count": {"type": "integer", "description": "How many keys are uploaded now."},
            "changed": {
                "type": "boolean",
                "description": "False when the provider already had exactly these keys.",
            },
            **_OPERATION_PROPS,
        },
        "required": ["pool_id", "provider_id", "name", "keys_before", "key_count", "changed"],
        "additionalProperties": False,
    },
    group_key=GROUP_WORKLOAD_IDENTITY,
    tags=(*_TAGS, "update", "workload-identity"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the outside issuer signs with a new key, for example after a Kubernetes "
            "cluster was rebuilt. Without the new key, Google stops trusting its tokens."
        ),
        "preconditions": (
            "A person must approve the call. The provider must exist and be an OIDC provider, "
            "and the login needs iam.workloadIdentityPoolProviders.update on the project; MEHO "
            "checks this before it asks for approval."
        ),
        "output_shape": (
            "{pool_id, provider_id, name, issuer_uri, keys_before, key_count, changed, "
            "operation_name, operation_done, note}. Key values are never returned."
        ),
        "next_step": (
            "Check the provider with gcloud.iam.workload_identity_pools.providers.list. Remove "
            "an old key later with another update_keys call when no token uses it any more."
        ),
    },
)

_SA_BINDING_ADD_OP = GcloudOp(
    op_id="gcloud.iam.service_accounts.binding.add",
    handler_attr="gcloud_sa_binding_add",
    summary="Add one member to one role on a service account's IAM policy.",
    description=(
        "Reads the service account policy, adds only the named member to the named role and "
        "sends setIamPolicy with the etag it read. A member that is already there is kept and "
        "nothing is sent. If the policy changed in between, MEHO changes nothing. Waits for "
        "approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "service_account_email": _SA_PROP,
            "role": {
                "type": "string",
                "pattern": ROLE_PATTERN,
                "description": "The role to give, e.g. roles/iam.workloadIdentityUser.",
            },
            "member": {
                "type": "string",
                "pattern": MEMBER_PATTERN,
                "maxLength": 1024,
                "description": (
                    "The member to add, e.g. principal://... or principalSet://... of a "
                    "workload identity pool, or serviceAccount:..., user:..., group:.... "
                    "allUsers, allAuthenticatedUsers and deleted: members are refused."
                ),
            },
        },
        "required": ["service_account_email", "role", "member"],
        "additionalProperties": False,
        "examples": [
            {
                "service_account_email": _EXAMPLE_SA,
                "role": "roles/iam.workloadIdentityUser",
                "member": _EXAMPLE_MEMBER,
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "service_account_email": {"type": "string", "description": "The service account."},
            "role": {"type": "string", "description": "The role."},
            "member": {"type": "string", "description": "The member."},
            "added": {
                "type": "boolean",
                "description": "True when the member was added; false when it was already there.",
            },
            "members_with_role": {
                "type": "integer",
                "description": "How many members hold the role without a condition now.",
            },
            "etag": {"type": ["string", "null"], "description": "The policy etag after the call."},
        },
        "required": ["service_account_email", "role", "member", "added", "members_with_role"],
        "additionalProperties": False,
    },
    group_key="iam",
    tags=(*_TAGS, "service-accounts", "policy"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to let one identity use or act as a service account, for example to give "
            "roles/iam.workloadIdentityUser to the principalSet of a workload identity pool."
        ),
        "preconditions": (
            "A person must approve the call. The login needs "
            "iam.serviceAccounts.getIamPolicy and iam.serviceAccounts.setIamPolicy on the "
            "account; MEHO checks this, and that the account exists, before it asks for "
            "approval. Only the binding without a condition is changed."
        ),
        "output_shape": (
            "{service_account_email, role, member, added, members_with_role, etag}. "
            "added=false means the member already held the role; nothing was sent."
        ),
        "next_step": (
            "If Google refuses because the policy changed after MEHO read it, MEHO does not "
            "retry: read the policy again with gcloud.iam.service_accounts.policy.read and "
            "call again. To undo, use gcloud.iam.service_accounts.binding.remove."
        ),
    },
)

#: The four operations, in the order they are registered.
WORKLOAD_IDENTITY_WRITE_OPS: Final[tuple[GcloudOp, ...]] = (
    _POOL_CREATE_OP,
    _PROVIDER_CREATE_OP,
    _UPDATE_KEYS_OP,
    _SA_BINDING_ADD_OP,
)
