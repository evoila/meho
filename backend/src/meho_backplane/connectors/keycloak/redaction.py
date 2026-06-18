# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret scrubbing for keycloak read-op output (G3.13-T2 #1394).

The curated read ops return the Admin REST representations an operator
would otherwise read with ``kcadm.sh get``. Those representations carry
**secret material** that has no place in an op result, an audit row, or
the broadcast feed:

* ``ClientRepresentation.secret`` — the confidential client's secret.
  Present on ``GET /admin/realms/{realm}/clients`` and
  ``GET /admin/realms/{realm}/clients/{id}`` for ``confidential`` clients.
* ``UserRepresentation.credentials`` — the list of credential records
  (password hashes, OTP seeds, etc.). The Admin API does not return these
  on the default ``GET /users`` listing, but it *can* on a single-user
  fetch or a future param change, so the read ops scrub it
  unconditionally rather than relying on Keycloak's default projection.
* ``secret`` on any nested representation (e.g. an OIDC identity-provider
  config carried inside a client) — caught by the recursive walk.

Why scrub at the connector boundary
====================================

MEHO's general posture is to redact secret material at the
classification / broadcast layer (the ``credential_mint`` /
``credential_write`` classifiers). That posture fits ops where the
secret *is* the payload the operator asked for (``vault.token.create``
returns the token; the broadcast redacts it but the caller still gets
it). The keycloak read ops are the opposite case: the secret is
**incidental** to a config read the operator never needs the secret for.
Returning the secret in the op ``result`` — even with downstream
broadcast redaction — would still place it in the synchronous response
the caller receives. So these ops scrub at the source: the secret never
enters the :class:`~meho_backplane.connectors.schemas.OperationResult` in
the first place.

The scrub is non-destructive to the operator's intent: every other field
of the representation (flows, redirect URIs, protocol mappers, scope
assignments, role mappings) passes through untouched.
"""

from __future__ import annotations

from typing import Any

__all__ = ["REDACTED", "redact_secret_fields"]

#: Sentinel written in place of a scrubbed secret value. A non-empty
#: marker (rather than dropping the key) keeps the shape stable so a
#: caller can see that a secret *existed* without learning its value.
REDACTED = "***REDACTED***"

#: Field names whose value (scalar **or** subtree) is secret material and
#: is replaced wholesale with :data:`REDACTED` wherever it appears in a
#: representation. Matched **case-insensitively** (lowercased here): a
#: representation is ``additionalProperties: true``, so a credential field
#: can arrive under any casing and the scrub must not depend on Keycloak's
#: camelCase convention to catch it. Replacing the whole value rather than
#: descending keeps a ``credentials`` *list* of hash records from leaking
#: any element, and a scalar ``secret`` from leaking its value.
#:
#: Two families of key are covered:
#:
#: * **Keycloak representation secret fields** — the camelCase keys the
#:   Admin REST representations use for credential material.
#: * **Generic credential param spellings** — the same well-known
#:   credential key names the generic params-echo default scrubs
#:   (:data:`~meho_backplane.operations._preview._SECRET_PARAM_KEYS`).
#:   Notably ``password``: a ``RealmRepresentation.smtpServer`` is a
#:   ``Map<String,String>`` that stores the SMTP relay password under the
#:   ``password`` key, and a representation is ``additionalProperties: true``
#:   so a ``password`` can appear anywhere. Including these here makes the
#:   bespoke keycloak preview builders (#1857) at least as strict as the
#:   generic echo they bypass, so no ``password``-keyed secret lands in the
#:   durable ``ApprovalRequest.proposed_effect`` row.
_SECRET_FIELDS: frozenset[str] = frozenset(
    {
        # Keycloak representation secret fields (camelCase, as Keycloak emits).
        "secret",  # ClientRepresentation.secret (confidential client secret)
        "credentials",  # UserRepresentation.credentials (list of credential records)
        "value",  # CredentialRepresentation.value (raw credential value)
        "secretdata",  # CredentialRepresentation.secretData (hash + salt blob)
        "credentialdata",  # CredentialRepresentation.credentialData (algo metadata)
        # Generic credential param spellings, mirroring the generic echo's
        # _SECRET_PARAM_KEYS so the bespoke path scrubs at least as much.
        # `password` closes the RealmRepresentation.smtpServer.password leak.
        "password",
        "passwd",
        "pwd",
        "client_secret",
        "secret_id",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "auth_token",
        "session_token",
        "private_key",
    }
)


def redact_secret_fields(value: Any) -> Any:
    """Return *value* with Keycloak secret material scrubbed, recursively.

    Walks dicts and lists. For every dict, a key matching
    :data:`_SECRET_FIELDS` (case-insensitively) has its whole value
    replaced with :data:`REDACTED`; every other value is walked
    recursively so a secret nested inside a protocol mapper, an
    identity-provider config, or a ``smtpServer`` map is still caught.

    Scalars (``str`` / ``int`` / ``bool`` / ``None``) pass through
    unchanged. The input is not mutated — a new structure is returned so
    the handler's scrub can never accidentally leave a half-redacted
    object aliased somewhere else.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in _SECRET_FIELDS
                else redact_secret_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secret_fields(item) for item in value]
    return value
