# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Where the password of ``vault.auth.userpass.write`` comes from.

The op takes exactly one of three sources:

* ``password`` -- the password is in the request (the old way);
* ``password_secret_ref`` -- MEHO reads the password from a KV-v2 path in the
  same Vault, so nobody has to pass it through an agent or a chat;
* ``generate_password`` -- MEHO makes a random password, writes it to a KV-v2
  path first, and then sets it on the login.

The password is never put in a result, an error text or a log line. Only the
place it is stored (mount, path, field) is reported.

A generated password is safe to run twice. When the login exists and the KV
path already holds a password, nothing changes, unless ``rotate`` is true.
The KV write comes first on purpose: a run that stops after it leaves the
password in KV, never a login whose password nobody knows. The KV write uses
``cas`` (the secret version), so a second writer cannot be overwritten.
"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass
from typing import Any

import hvac.exceptions

__all__ = [
    "DEFAULT_PASSWORD_FIELD",
    "DEFAULT_PASSWORD_LENGTH",
    "DEFAULT_SECRET_MOUNT",
    "PasswordPlan",
    "PasswordSecretError",
    "apply_credential_source",
    "generate_password_value",
    "select_credential_source",
]

DEFAULT_SECRET_MOUNT = "secret"
DEFAULT_PASSWORD_FIELD = "password"
DEFAULT_PASSWORD_LENGTH = 32

_ALPHABET = string.ascii_letters + string.digits + "-_"


class PasswordSecretError(Exception):
    """The KV secret that should hold a password is missing or malformed.

    The message names the mount, path and field. It never holds a value.
    """


@dataclass(frozen=True, slots=True)
class PasswordPlan:
    """The password source of one call, with no password value in it."""

    source: str  # "request" | "kv_path" | "generated"
    mount: str = DEFAULT_SECRET_MOUNT
    path: str = ""
    field: str = DEFAULT_PASSWORD_FIELD
    length: int = DEFAULT_PASSWORD_LENGTH
    rotate: bool = False

    @property
    def location(self) -> dict[str, str] | None:
        """Where the password is stored (no value), or ``None`` for inline."""
        if self.source == "request":
            return None
        return {"mount": self.mount, "path": self.path, "field": self.field}


def select_credential_source(params: dict[str, Any]) -> PasswordPlan:
    """Check that exactly one password source is given, and describe it."""
    given = [
        key for key in ("password", "password_secret_ref", "generate_password") if key in params
    ]
    if len(given) != 1:
        raise ValueError(
            "give exactly one of password, password_secret_ref and generate_password "
            f"(got {len(given)})"
        )
    rotate = bool(params.get("rotate", False))
    if rotate and given[0] != "generate_password":
        raise ValueError("rotate only works together with generate_password")

    if given[0] == "password":
        return PasswordPlan(source="request")
    if given[0] == "password_secret_ref":
        return PasswordPlan(
            source="kv_path",
            mount=str(params.get("password_secret_mount") or DEFAULT_SECRET_MOUNT).strip(),
            path=_clean_path(params["password_secret_ref"], "password_secret_ref"),
            field=str(params.get("password_secret_key") or DEFAULT_PASSWORD_FIELD).strip(),
        )
    spec = params["generate_password"]
    if not isinstance(spec, dict):
        raise ValueError("generate_password must be an object with a path")
    return PasswordPlan(
        source="generated",
        mount=str(spec.get("mount") or DEFAULT_SECRET_MOUNT).strip(),
        path=_clean_path(spec.get("path"), "generate_password.path"),
        field=str(spec.get("field") or DEFAULT_PASSWORD_FIELD).strip(),
        length=int(spec.get("length") or DEFAULT_PASSWORD_LENGTH),
        rotate=rotate,
    )


def _clean_path(value: Any, name: str) -> str:
    path = str(value or "").strip().strip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"{name} must be a KV path such as 'labs/alice/login'")
    return path


def generate_password_value(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """Make a random password with a lower case, an upper case and a digit."""
    while True:
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
        ):
            return candidate


def _read_kv(client: Any, plan: PasswordPlan) -> tuple[dict[str, Any], int] | None:
    """Return ``(data, version)`` of the KV secret, or ``None`` when absent."""
    try:
        payload = client.secrets.kv.v2.read_secret_version(
            path=plan.path, mount_point=plan.mount, raise_on_deleted_version=True
        )
    except hvac.exceptions.InvalidPath:
        return None
    data = payload["data"]["data"]
    version = int(payload["data"]["metadata"]["version"])
    return (data if isinstance(data, dict) else {}), version


def _stored_password(found: tuple[dict[str, Any], int] | None, plan: PasswordPlan) -> str | None:
    if found is None:
        return None
    value = found[0].get(plan.field)
    if isinstance(value, str) and value:
        return value
    return None


def _write_kv(
    client: Any, plan: PasswordPlan, found: tuple[dict[str, Any], int] | None, value: str
) -> None:
    """Store the password. Keep the other fields; refuse a lost race (``cas``)."""
    data = dict(found[0]) if found else {}
    data[plan.field] = value
    try:
        client.secrets.kv.v2.create_or_update_secret(
            path=plan.path,
            secret=data,
            cas=found[1] if found else 0,
            mount_point=plan.mount,
        )
    except (hvac.exceptions.InvalidRequest, hvac.exceptions.InvalidPath) as exc:
        raise PasswordSecretError(
            f"could not store the password at KV {plan.mount!r} path {plan.path!r}: "
            "check that the KV-v2 mount exists and that the secret is not "
            "soft-deleted (undelete or destroy it, then run again)"
        ) from exc


def _user_policies(client: Any, username: str, mount: str) -> list[str] | None:
    """Return the policies of the login, or ``None`` when the login is absent."""
    try:
        payload = client.auth.userpass.read_user(username=username, mount_point=mount)
    except hvac.exceptions.InvalidPath:
        return None
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    policies = data.get("token_policies") or data.get("policies") or []
    return [str(p) for p in policies]


def _set_user(
    client: Any,
    *,
    username: str,
    mount: str,
    password: str | None,
    token_policies: list[str] | None,
) -> None:
    kwargs: dict[str, Any] = {"username": username, "mount_point": mount}
    if password is not None:
        kwargs["password"] = password
    if token_policies is not None:
        kwargs["token_policies"] = token_policies
    client.auth.userpass.create_or_update_user(**kwargs)


def _apply_sync(
    client: Any,
    plan: PasswordPlan,
    *,
    username: str,
    mount: str,
    token_policies: list[str] | None,
    inline_password: str | None,
) -> dict[str, Any]:
    """Run the whole write with the blocking hvac client (call in a thread)."""
    if plan.source == "request":
        _set_user(
            client,
            username=username,
            mount=mount,
            password=inline_password,
            token_policies=token_policies,
        )
        return {"changed": True, "credential_generated": False}

    found = _read_kv(client, plan)
    stored = _stored_password(found, plan)

    if plan.source == "kv_path":
        if stored is None:
            raise PasswordSecretError(
                f"no password at KV {plan.mount!r} path {plan.path!r} field {plan.field!r}"
            )
        _set_user(
            client, username=username, mount=mount, password=stored, token_policies=token_policies
        )
        return {"changed": True, "credential_generated": False}

    # generated
    current_policies = _user_policies(client, username, mount)
    if current_policies is not None and stored is not None and not plan.rotate:
        # Safe to run twice: the login exists and its password is stored.
        if token_policies is not None and sorted(token_policies) != sorted(current_policies):
            _set_user(
                client, username=username, mount=mount, password=None, token_policies=token_policies
            )
            return {"changed": True, "credential_generated": False}
        return {"changed": False, "credential_generated": False}

    generated = False
    password = stored
    if password is None or plan.rotate:
        password = generate_password_value(plan.length)
        _write_kv(client, plan, found, password)
        generated = True
    _set_user(
        client, username=username, mount=mount, password=password, token_policies=token_policies
    )
    return {"changed": True, "credential_generated": generated}


async def apply_credential_source(
    client: Any,
    plan: PasswordPlan,
    *,
    username: str,
    mount: str,
    token_policies: list[str] | None,
    inline_password: str | None,
) -> dict[str, Any]:
    """Write the login with the planned password source; return a value-free summary."""
    return await asyncio.to_thread(
        _apply_sync,
        client,
        plan,
        username=username,
        mount=mount,
        token_policies=token_policies,
        inline_password=inline_password,
    )
