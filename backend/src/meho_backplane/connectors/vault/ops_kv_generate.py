# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``vault.kv.generate``: make a random password and store it in KV, never show it.

An operator needs a new password for a login on another product. The product
has its own password rules (length, how many digits, which special
characters, how many equal characters may follow each other). MEHO makes a
password that meets the rules with Python's ``secrets`` module, writes it to
one field of a KV-v2 secret and returns only the place: mount, path, field
and version. Other operations then read the password by reference.

* The value is never in the result, an error text, the approval preview or a
  log line. The params hold no secret, so the audit row has none either.
* Other fields at the path stay as they are (a ``username`` field can sit
  next to the password).
* Safe to run twice: when the field already holds a value, nothing changes,
  unless ``rotate`` is true. Then MEHO writes a new version.
* The write uses ``cas`` (the secret version), so a second writer is not
  overwritten.

The read, the write and the stored-value check are shared with the userpass
password source (:mod:`~meho_backplane.connectors.vault.userpass_password`).
The rules and the generator are in this module.

The result key names (``mount``, ``path``, ``field``, ``version``) avoid the
words ``password``, ``secret`` and ``token`` on purpose: the result redaction
hides values under such keys.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass
from typing import Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops import (
    _DEFAULT_KV_MOUNT,
    _MOUNT_PROPERTY,
    _PATH_PROPERTY,
    _extract_mount_and_path,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.connectors.vault.tenant_scope import enforce_tenant_scope
from meho_backplane.connectors.vault.userpass_password import (
    DEFAULT_PASSWORD_FIELD,
    PasswordPlan,
    _read_kv,
    _stored_password,
    _write_kv,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "GENERATE_OP_ID",
    "PasswordRuleError",
    "PasswordRules",
    "generate_with_rules",
    "register_vault_kv_generate_operation",
    "vault_kv_generate",
]

GENERATE_OP_ID = "vault.kv.generate"

DEFAULT_LENGTH = 24
DEFAULT_SPECIAL_CHARS = "!@#$%^&*-_=+"
_MAX_TRIES = 2000


class PasswordRuleError(ValueError):
    """The rules cannot be met, or are not valid. The message holds no value."""


@dataclass(frozen=True, slots=True)
class PasswordRules:
    """What a made password must meet."""

    length: int = DEFAULT_LENGTH
    min_upper: int = 1
    min_lower: int = 1
    min_digits: int = 1
    min_special: int = 0
    special_chars: str = DEFAULT_SPECIAL_CHARS
    max_repeat: int | None = None

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> PasswordRules:
        """Read the rules from the op params and check that they can be met."""
        max_repeat = params.get("max_repeat")
        rules = cls(
            length=int(params.get("length", DEFAULT_LENGTH)),
            min_upper=int(params.get("min_upper", 1)),
            min_lower=int(params.get("min_lower", 1)),
            min_digits=int(params.get("min_digits", 1)),
            min_special=int(params.get("min_special", 0)),
            special_chars=str(params.get("special_chars", DEFAULT_SPECIAL_CHARS)),
            max_repeat=int(max_repeat) if max_repeat is not None else None,
        )
        rules.check()
        return rules

    def check(self) -> None:
        """Raise :class:`PasswordRuleError` when no password can meet the rules."""
        minimums = (self.min_upper, self.min_lower, self.min_digits, self.min_special)
        if self.length < 1 or any(m < 0 for m in minimums):
            raise PasswordRuleError("length must be at least 1 and the minimums cannot be negative")
        if sum(minimums) > self.length:
            raise PasswordRuleError(
                f"the minimums add up to {sum(minimums)}, more than the length {self.length}"
            )
        bad = sorted({c for c in self.special_chars if c not in string.punctuation})
        if bad:
            raise PasswordRuleError(
                "special_chars may only hold printable punctuation characters "
                f"(found {len(bad)} other character(s))"
            )
        if self.min_special > 0 and not self.special_chars:
            raise PasswordRuleError("min_special needs at least one character in special_chars")
        if self.max_repeat is not None:
            if self.max_repeat < 1:
                raise PasswordRuleError("max_repeat must be at least 1")
            if len(self._pool()) < 2:
                raise PasswordRuleError(
                    "max_repeat needs at least two different allowed characters"
                )

    def _pool(self) -> str:
        return string.ascii_letters + string.digits + "".join(sorted(set(self.special_chars)))

    def shown(self) -> dict[str, Any]:
        """The rules as plain data, for the approval preview. Holds no value."""
        return {
            "length": self.length,
            "min_upper": self.min_upper,
            "min_lower": self.min_lower,
            "min_digits": self.min_digits,
            "min_special": self.min_special,
            "special_chars": self.special_chars,
            "max_repeat": self.max_repeat,
        }


def _longest_run(value: str) -> int:
    longest = run = 0
    previous = ""
    for char in value:
        run = run + 1 if char == previous else 1
        previous = char
        longest = max(longest, run)
    return longest


def generate_with_rules(rules: PasswordRules) -> str:
    """Make a random password that meets *rules*, with a cryptographic random source."""
    rng = secrets.SystemRandom()
    pool = rules._pool()
    for _ in range(_MAX_TRIES):
        chars = (
            [secrets.choice(string.ascii_uppercase) for _ in range(rules.min_upper)]
            + [secrets.choice(string.ascii_lowercase) for _ in range(rules.min_lower)]
            + [secrets.choice(string.digits) for _ in range(rules.min_digits)]
            + [secrets.choice(rules.special_chars) for _ in range(rules.min_special)]
        )
        chars += [secrets.choice(pool) for _ in range(rules.length - len(chars))]
        rng.shuffle(chars)
        candidate = "".join(chars)
        if rules.max_repeat is None or _longest_run(candidate) <= rules.max_repeat:
            return candidate
    raise PasswordRuleError(
        "could not make a password that meets the repeat limit; relax the rules"
    )


def _generate_sync(client: Any, plan: PasswordPlan, rules: PasswordRules) -> dict[str, Any]:
    """Read, maybe write, with the blocking hvac client (call in a thread)."""
    found = _read_kv(client, plan)
    if _stored_password(found, plan) is not None and not plan.rotate:
        # Safe to run twice: the field already holds a value.
        version = found[1] if found else 0
        return {"version": version, "generated": False}
    version = _write_kv(client, plan, found, generate_with_rules(rules))
    return {"version": version, "generated": True}


async def vault_kv_generate(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Make a password that meets the rules and store it in one KV-v2 field.

    Op-id: ``vault.kv.generate``. Returns the place and the version, never the
    value.
    """
    mount, path = _extract_mount_and_path(GENERATE_OP_ID, params)
    field = str(params.get("field") or DEFAULT_PASSWORD_FIELD).strip()
    rules = PasswordRules.from_params(params)
    rotate = bool(params.get("rotate", False))
    # Mutating op: the read-only platform-path allow-list does not apply.
    enforce_tenant_scope(operator, mount=mount, path=path, read_only=False)
    plan = PasswordPlan(
        source="generated",
        mount=mount,
        path=path.strip("/"),
        field=field,
        length=rules.length,
        rotate=rotate,
    )
    async with vault_client_for_target(operator, target) as client:
        outcome = await asyncio.to_thread(_generate_sync, client, plan, rules)
    return {
        "mount": mount,
        "path": plan.path,
        "field": field,
        "version": outcome["version"],
        "generated": outcome["generated"],
    }


async def _generate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval card: the place, the field and the rules. Never a value.

    Pure: reads only the params. A rule set that cannot be met shows no rules
    and the run then refuses it.
    """
    mount = str(ctx.params.get("mount", _DEFAULT_KV_MOUNT)).strip()
    path = str(ctx.params.get("path", "")).strip()
    preview: dict[str, Any] = {
        "resource": "kv_secret",
        "mount": mount,
        "path": path,
        "field": str(ctx.params.get("field") or DEFAULT_PASSWORD_FIELD).strip(),
        "kv_version": 2,
        "semantics": "rotate" if ctx.params.get("rotate") else "create_if_missing",
        "note": (
            "MEHO makes a random value and writes it to this field. The value is "
            "never shown. Other fields at the path stay as they are."
        ),
    }
    try:
        preview["rules"] = PasswordRules.from_params(ctx.params).shown()
    except (ValueError, TypeError) as exc:
        preview["rules_error"] = str(exc)
    return preview


GENERATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": _MOUNT_PROPERTY,
        "path": _PATH_PROPERTY,
        "field": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "default": DEFAULT_PASSWORD_FIELD,
            "description": "Field of the KV secret that holds the password. Default 'password'.",
        },
        "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 256,
            "default": DEFAULT_LENGTH,
            "description": "Exact length of the password. Match the product's limit.",
        },
        "min_upper": {
            "type": "integer",
            "minimum": 0,
            "default": 1,
            "description": "The smallest number of upper-case letters.",
        },
        "min_lower": {
            "type": "integer",
            "minimum": 0,
            "default": 1,
            "description": "The smallest number of lower-case letters.",
        },
        "min_digits": {
            "type": "integer",
            "minimum": 0,
            "default": 1,
            "description": "The smallest number of digits.",
        },
        "min_special": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "The smallest number of special characters.",
        },
        "special_chars": {
            "type": "string",
            "default": DEFAULT_SPECIAL_CHARS,
            "description": (
                "The special characters that may be used. Give an empty string when "
                "the product allows none. Default: " + DEFAULT_SPECIAL_CHARS
            ),
        },
        "max_repeat": {
            "type": "integer",
            "minimum": 1,
            "description": "The longest run of one repeated character. Omit for no limit.",
        },
        "rotate": {
            "type": "boolean",
            "default": False,
            "description": (
                "true writes a new value even when the field already holds one. "
                "Default false: then nothing changes."
            ),
        },
    },
    "required": ["path"],
    "additionalProperties": False,
    "examples": [
        {"path": "labs/<name>/login"},
        {
            "path": "labs/<name>/login",
            "length": 20,
            "min_special": 1,
            "special_chars": "!@#$",
            "max_repeat": 2,
        },
    ],
}

GENERATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string"},
        "path": {"type": "string"},
        "field": {"type": "string"},
        "version": {"type": "integer", "description": "KV version that holds the value."},
        "generated": {
            "type": "boolean",
            "description": "false when the field already held a value and nothing changed.",
        },
    },
    "required": ["mount", "path", "field", "version", "generated"],
}

_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "You need a new password for a login on another product, and nobody should see it."
    ),
    "preconditions": (
        "Know the product's password rules (length, special characters, "
        "repeat limit). The KV path must be one the caller may write."
    ),
    "next_step": (
        "Pass the same path and field as a reference to the operation that sets "
        "the password on the product, for example a user create."
    ),
    "when_to_use": (
        "Make a new random password that meets a product's password rules and "
        "store it in Vault KV, so nobody has to see it. Other operations then "
        "use the password by reference (the path and field). Use it before "
        "creating a login on another product."
    ),
    "parameter_hints": {
        "path": "Required. KV path that will hold the password.",
        "field": "Optional. Field name. Default 'password'. Other fields stay.",
        "length": "Match the product's limit, for example 20 for vCenter single sign-on.",
        "min_special": "Set 1 or more when the product needs a special character.",
        "special_chars": "Limit the special characters to what the product accepts.",
        "max_repeat": "Set it when the product limits equal characters in a row.",
        "rotate": "true makes a new version even when the field has a value.",
    },
    "output_shape": (
        "{'mount', 'path', 'field', 'version', 'generated'}. Never the password. "
        "generated=false means the field already had a value and nothing changed."
    ),
}


register_preview_builder(GENERATE_OP_ID, _generate_preview)


async def register_vault_kv_generate_operation(
    *,
    when_to_use: str,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert ``vault.kv.generate`` into the ``kv`` group."""
    await register_typed_operation(
        product="vault",
        version="1.x",
        impl_id="vault",
        group_key="kv",
        when_to_use=when_to_use,
        embedding_service=embedding_service,
        op_id=GENERATE_OP_ID,
        handler=vault_kv_generate,
        summary="Make a random password that meets given rules and store it in a KV v2 field.",
        description=(
            "Makes a random password that meets the given rules and writes it to "
            "one field of a KV v2 secret. Returns only the mount, path, field and "
            "version, never the value. Safe to run twice unless rotate is true."
        ),
        parameter_schema=GENERATE_PARAMETER_SCHEMA,
        response_schema=GENERATE_RESPONSE_SCHEMA,
        tags=["write", "secret-write"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=_LLM_INSTRUCTIONS,
    )
