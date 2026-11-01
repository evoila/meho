# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How ``vmware.appliance.local_account.password.set`` keeps the new password secret.

All code that touches the new password is in this module:

* :func:`read_new_password` reads it from the Vault path the call names
  (``password_secret_ref``), with
  :func:`~meho_backplane.connectors._shared.vault_creds.load_param_secret_data`,
  under the identity of the person whose call runs (after an approval: the
  approver). The password is never an operation parameter. The Keycloak and
  VCF Automation user writes use the same seam.
* :func:`change_error` turns a vCenter error answer to either change (the
  password change and the expiry change) into an error with no raw vendor
  bytes: no original response, no original request, and not the original
  error as its context. It passes on the vAPI error type and messages, and
  drops all vendor text when any of it contains the password. A 401 stays an
  ``httpx.HTTPStatusError`` with status 401 (:func:`sanitized_status_error`),
  so the dispatcher still logs in again and retries once.
* :func:`password_set_preview` is the approval preview. The op is pinned as
  ``credential_write`` in :mod:`meho_backplane.broadcast.events` (so the flight
  recorder keeps no body of the call, the broadcast carries no params, and a
  waiting approval keeps the params in its encrypted store). That pin also
  hides the params from the approver, so the preview shows the account and the
  Vault path, never the password.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors._shared.secret_echo import mentions_secret, response_text
from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.base import ConnectorResourceNotFoundError, ConnectorUpstreamError
from meho_backplane.operations import register_plain_fields
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vmware_rest.session import VsphereTargetLike

__all__ = [
    "DEFAULT_PASSWORD_KEY",
    "PASSWORD_SET_OP_ID",
    "LocalAccountPasswordSecretError",
    "LocalAccountRefusedError",
    "change_error",
    "password_set_preview",
    "read_new_password",
    "safe_vendor_text",
    "sanitized_status_error",
    "vapi_error",
]

PASSWORD_SET_OP_ID: Final = "vmware.appliance.local_account.password.set"

#: The key in the Vault secret that holds the new password, unless the call names another.
DEFAULT_PASSWORD_KEY: Final = "password"

_MAX_VENDOR_MESSAGES: Final = 3
_MAX_VENDOR_TEXT: Final = 300
_PERMISSION_HINT: Final = (
    " The service account needs the appliance super administrator level: membership "
    "in the SSO group SystemConfiguration.BashShellAdministrators."
)


class LocalAccountPasswordSecretError(Exception):
    """The Vault secret named for the new password has no usable text under its key.

    The message names the path and the key, never a value.
    """


class LocalAccountRefusedError(ConnectorUpstreamError):
    """vCenter refused a local-account call; the error names the SSO group the account needs.

    ``remediation`` holds the SSO group hint. The dispatcher copies it into
    the result in full, also when it cuts the message short.
    """

    def __init__(
        self, message: str, *, http_status: int, upstream_message: str | None, remediation: str
    ) -> None:
        super().__init__(message, http_status=http_status, upstream_message=upstream_message)
        self.remediation = remediation


@dataclass(slots=True)
class _PasswordRefTarget:
    """Points the shared Vault read at the call's ``password_secret_ref``.

    Carries the target's name and host for log lines; the ref is the call's,
    not the target's own login secret. Not frozen: the structural Protocol
    of the shared Vault read declares settable members.
    """

    name: str
    host: str
    secret_ref: str | None


def _password_ref(params: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(secret_ref, mount, key)`` of the Vault secret that holds the new password."""
    return (
        str(params.get("password_secret_ref") or "").strip(),
        str(params.get("password_secret_mount") or DEFAULT_KV_MOUNT).strip(),
        str(params.get("password_secret_key") or DEFAULT_PASSWORD_KEY).strip(),
    )


async def read_new_password(
    operator: Operator, target: VsphereTargetLike, params: Mapping[str, Any]
) -> str:
    """Read the new password from ``password_secret_ref`` under the caller's identity.

    A Vault refusal or a missing path raises
    :class:`~meho_backplane.connectors._shared.vault_creds.SecretRefReadError`
    (the dispatcher answers ``vault_credentials_read_error``). A secret without
    text under the key raises :class:`LocalAccountPasswordSecretError`. Either
    way nothing is sent to vCenter, and no message holds a secret value.
    """
    secret_ref, mount, key = _password_ref(params)
    if not secret_ref:
        raise LocalAccountPasswordSecretError(
            "password_secret_ref is empty: name the Vault path that holds the new password"
        )
    data = await load_param_secret_data(
        _PasswordRefTarget(name=target.name, host=target.host, secret_ref=secret_ref),
        operator,
        param="password_secret_ref",
        mount=mount,
    )
    value = data.get(key) if isinstance(data, dict) else None
    password = strip_credential_value(value) if isinstance(value, str) else ""
    if not password:
        raise LocalAccountPasswordSecretError(
            f"the Vault secret at password_secret_ref={secret_ref!r} (mount={mount!r}) has "
            f"no text under the key {key!r}. Store the new password under that key, or "
            "set password_secret_key. Nothing was sent to vCenter."
        )
    return password


def vapi_error(response: httpx.Response) -> tuple[str | None, list[str]]:
    """Return the vAPI error type and the vendor's message texts of an error answer.

    Reads the ``/api`` shape (``error_type``, ``messages``) and the legacy
    ``/rest`` shape (``type``, ``value.messages``). Each text is cut to 300
    characters, and at most 3 messages are kept.
    """
    try:
        body = response.json()
    except (ValueError, httpx.StreamError):
        return None, []
    if not isinstance(body, dict):
        return None, []
    error_type = body.get("error_type") or body.get("type")
    value = body.get("value")
    container = value if isinstance(value, dict) else body
    raw_messages = container.get("messages")
    messages = [
        message["default_message"][:_MAX_VENDOR_TEXT]
        for message in (raw_messages if isinstance(raw_messages, list) else [])
        if isinstance(message, dict) and isinstance(message.get("default_message"), str)
    ]
    kept_type = error_type[:_MAX_VENDOR_TEXT] if isinstance(error_type, str) else None
    return kept_type, messages[:_MAX_VENDOR_MESSAGES]


def safe_vendor_text(
    response: httpx.Response, secrets: Iterable[str] = ()
) -> tuple[str | None, list[str]]:
    """The vAPI error type and messages of *response*, or nothing when a secret is in them.

    Checks the raw body and the parsed texts against each secret, as is,
    JSON-escaped, URL-encoded and HTML-escaped. When any vendor text holds a
    secret, all of it is dropped. Empty secrets are ignored.
    """
    error_type, messages = vapi_error(response)
    texts = [response_text(response), error_type or "", *messages]
    if any(mentions_secret(texts, secret) for secret in secrets if secret):
        return None, []
    return error_type, messages


def sanitized_status_error(
    exc: httpx.HTTPStatusError, *, secrets: Iterable[str] = ()
) -> httpx.HTTPStatusError:
    """A copy of *exc* with the same class and status, and no raw vendor bytes.

    The dispatcher's error handling reads the status (a 401 starts its login
    retry) and the response body (copied into the result and the audit row).
    So the copy's response holds only :func:`safe_vendor_text`, and its request
    is built from the method and the URL alone: it has neither the session
    header nor the body (the new password).
    """
    status = exc.response.status_code
    request = httpx.Request(exc.request.method, exc.request.url)
    error_type, messages = safe_vendor_text(exc.response, secrets)
    body: dict[str, Any] = {}
    if error_type:
        body["error_type"] = error_type
    if messages:
        body["messages"] = [{"default_message": message} for message in messages]
    response = (
        httpx.Response(status, json=body, request=request)
        if body
        else httpx.Response(status, request=request)
    )
    return httpx.HTTPStatusError(
        f"vCenter answered HTTP {status} for {request.method} {request.url.path}",
        request=request,
        response=response,
    )


def change_error(
    exc: httpx.HTTPStatusError,
    *,
    op_id: str,
    action: str,
    username: str,
    secrets: Iterable[str] = (),
) -> Exception:
    """The error a change raises for a vCenter error answer; it holds no raw vendor bytes.

    * 404: :class:`~meho_backplane.connectors.base.ConnectorResourceNotFoundError`
      (the dispatcher answers ``not_found``).
    * 401: :func:`sanitized_status_error`, so the dispatcher still logs in
      again and retries once.
    * Any other status: :class:`~meho_backplane.connectors.base.ConnectorUpstreamError`
      with the status and :func:`safe_vendor_text`. A 403 is a
      :class:`LocalAccountRefusedError` that also names the SSO group the
      service account needs.

    The caller raises the result after its ``except`` block, so the new error
    does not keep the original one (its response and its request) as context.
    """
    secrets = tuple(secrets)
    status = exc.response.status_code
    if status == 404:
        return ConnectorResourceNotFoundError(
            [username], f"vCenter has no local account named {username!r}"
        )
    if status == 401:
        return sanitized_status_error(exc, secrets=secrets)
    error_type, messages = safe_vendor_text(exc.response, secrets)
    detail = "; ".join(part for part in (error_type, *messages) if part) or None
    message = (
        f"{op_id}: vCenter answered HTTP {status} when MEHO tried to {action} for {username!r}."
    )
    if status == 403:
        return LocalAccountRefusedError(
            f"{message}{_PERMISSION_HINT}",
            http_status=status,
            upstream_message=detail,
            remediation=_PERMISSION_HINT.strip(),
        )
    return ConnectorUpstreamError(message, http_status=status, upstream_message=detail)


async def password_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """What the approver sees for a password change: the account and the Vault path.

    The Vault path is not a secret; the password is never read here.
    """
    secret_ref, mount, key = _password_ref(ctx.params)
    username = ctx.params.get("username")
    return {
        "change": "set a new password for one local account of the vCenter appliance",
        "username": username if isinstance(username, str) else None,
        "password_source": "vault",
        "password_secret_ref": secret_ref or None,
        "password_secret_mount": mount,
        "password_secret_key": key,
    }


register_preview_builder(PASSWORD_SET_OP_ID, password_set_preview)


# The date the new password expires, never the password.
register_plain_fields(PASSWORD_SET_OP_ID, ("password_expires_at",))
