# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema documents for the synthetic ``targets.register`` typed op.

Single source for both the ``endpoint_descriptor.parameter_schema`` (the
dispatcher's validation layer) and the ``meho_targets_register`` MCP
tool's ``inputSchema`` — sharing one object means the two validation
layers cannot drift (the mold the topology graph ops use).

The parameter schema mirrors
:class:`~meho_backplane.targets.schemas.TargetCreate` field-for-field so
an MCP client schema-validating ahead of the call sees the same
constraints the service enforces: required ``name`` / ``product`` /
``host``; the documented optional set; ``additionalProperties: false`` so
the two server-managed inputs the create body forbids —
``fingerprint`` (probe-written) and ``tenant_id`` (JWT-derived) — are
rejected at the schema layer before the handler runs. ``vpn_required`` is
deliberately not exposed on the agent surface (it is a network-topology
concern, not an onboarding parameter); an omitted value takes the
``TargetCreate`` default.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "TARGETS_REGISTER_PARAMETER_SCHEMA",
    "TARGETS_REGISTER_RESPONSE_SCHEMA",
]

#: ``auth_model`` accepted values, materialised from the enum so the
#: schema tracks :class:`~meho_backplane.connectors.schemas.AuthModel`
#: automatically rather than hard-coding a list that could drift.
_AUTH_MODEL_VALUES: Final[list[str]] = [m.value for m in AuthModel]


TARGETS_REGISTER_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": (
                "Unique target name within the tenant. Immutable after "
                "creation (rename = delete + re-create). This is the "
                "handle every dispatch resolves against (`--target <name>`)."
            ),
        },
        "product": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": (
                "Product token the target speaks, validated against the "
                "registered connector products (e.g. `vmware`, `vault`, "
                "`rke2`, `bind9`). An unknown product is rejected with the "
                "valid set named. Copy it straight out of "
                "`search_connectors` / `list_connectors`."
            ),
        },
        "host": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "description": (
                "Dialable host or IP. Screened by the SSRF guard: a "
                "non-public destination (private / loopback / link-local / "
                "metadata / CGNAT) is rejected unless the operator "
                "allowlist exempts it."
            ),
        },
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Alternate names the target also resolves under.",
        },
        "version": {
            "type": ["string", "null"],
            "maxLength": 100,
            "description": (
                "Optional operator-asserted product version (e.g. `9.0`). "
                "Lets the first probe dispatch against the versioned "
                "connector without a PATCH round-trip; omitted resolves "
                "via the connector's wildcard registration."
            ),
        },
        "port": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 65535,
            "description": "Optional TCP port; omitted uses the connector default.",
        },
        "fqdn": {
            "type": ["string", "null"],
            "description": (
                "Optional fully-qualified domain name. Screened by the same SSRF guard as `host`."
            ),
        },
        "secret_ref": {
            "type": ["string", "null"],
            "description": (
                "Optional Vault path to the target's credential. Must lie "
                "within the tenant's secret subtree — an out-of-scope ref "
                "is rejected. Omitting it derives the per-tenant default."
            ),
        },
        "auth_model": {
            "type": "string",
            "enum": _AUTH_MODEL_VALUES,
            "description": ("Per-target identity model. Defaults to `shared_service_account`."),
        },
        "verify_tls": {
            "type": "boolean",
            "description": (
                "Whether dispatch verifies the target's TLS certificate "
                "chain. Default-secure `true`; setting `false` is a "
                "security-relevant choice that is audited. Mutually "
                "exclusive with `tls_ca_pin`."
            ),
        },
        "tls_ca_pin": {
            "type": ["string", "null"],
            "description": (
                "Optional PEM CA bundle to trust for this target (the "
                "secure way to reach a self-signed / internal-CA "
                "appliance — verification stays on against the pin). "
                "Mutually exclusive with `verify_tls=false`."
            ),
        },
        "tls_server_name": {
            "type": ["string", "null"],
            "maxLength": 512,
            "description": (
                "Optional TLS SNI / cert-verification hostname, decoupled "
                "from `host` (for an appliance whose cert pins an FQDN-CN "
                "while it only accepts `Host: <IP>`). Omitted derives it "
                "from `host`."
            ),
        },
        "extras": {
            "type": "object",
            "description": "Optional free-form connector-specific metadata.",
        },
        "notes": {
            "type": ["string", "null"],
            "description": "Optional free-text operator notes.",
        },
        "preferred_impl_id": {
            "type": ["string", "null"],
            "maxLength": 200,
            "description": (
                "Optional connector-implementation override, validated "
                "against the impls registered for `product`."
            ),
        },
    },
    "required": ["name", "product", "host"],
    "additionalProperties": False,
}


TARGETS_REGISTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {
            "type": "string",
            "description": "UUID of the newly-registered target row.",
        },
        "name": {"type": "string"},
        "product": {"type": "string"},
        "host": {"type": "string"},
        "tenant_id": {
            "type": "string",
            "description": (
                "Owning tenant — taken from the caller's identity, never "
                "from params (cross-tenant registration is structurally "
                "impossible)."
            ),
        },
    },
    "required": ["target_id", "name", "product", "host", "tenant_id"],
}
