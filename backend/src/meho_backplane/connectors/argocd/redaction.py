# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structural read-side redaction of ArgoCD repository credentials (#3501).

``argocd.repo.list`` (``GET /api/v1/repositories``) returns ``Repository``
objects that can echo the credentials an operator configured for a repo:
a ``password`` (HTTPS basic), an ``sshPrivateKey``, a client-cert
``tlsClientCertKey``, a ``bearerToken``, or a ``githubAppPrivateKey``.
argocd-server returns these inline on the list for a broad-RBAC token, so
a safe read would surface them verbatim -- the ArgoCD half of the
read-side payload gap meho-internal#322 surfaced for the Envision visitor
tenant.

This is the argocd per-connector redactor (the same shape as
``connectors/keycloak/redaction.py`` and ``connectors/rabbitmq/redact.py``):
a pure, structural walk that blanks a fixed set of credential **field
names** wherever they appear, wholesale, so no repo secret rides back in
the response envelope / audit row / broadcast feed. Non-credential fields
(``repo``, ``username``, ``type``, ``name``, ``project``,
``connectionState``, ``tlsClientCertData`` -- the cert, not the key,
``githubAppId``) pass through so the read stays legible.

``argocd.cluster.list``'s own destination-cluster credentials are handled
separately and more aggressively by the handler, which drops the whole
``Cluster.config`` subtree (#2855); this redactor is the repository
complement.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ARGOCD_CREDENTIAL_FIELDS", "REDACTED", "redact_argocd_credentials"]

#: Placeholder substituted for a redacted credential value. Non-empty so a
#: caller sees the field *existed* without learning its value.
REDACTED = "***REDACTED***"

#: Repository / repo-credential secret field names ArgoCD can echo on a
#: read. Matched **case-insensitively** (lowercased here) so a spelling
#: drift never dodges the scrub; the value is replaced wholesale (the
#: subtree is not descended into) so a structured credential can never
#: leak an element.
ARGOCD_CREDENTIAL_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "sshprivatekey",
        "tlsclientcertkey",
        "bearertoken",
        "githubappprivatekey",
    }
)


def redact_argocd_credentials(value: Any) -> Any:
    """Return *value* with ArgoCD repository credential fields blanked.

    Recursively walks dicts and lists. A mapping key matching
    :data:`ARGOCD_CREDENTIAL_FIELDS` (case-insensitively) has its whole
    value replaced with :data:`REDACTED`; every other value is walked so
    a credential nested inside an ``items`` array or a wrapping envelope
    is still caught. Scalars pass through unchanged. The input is never
    mutated -- a new structure is returned.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in ARGOCD_CREDENTIAL_FIELDS
                else redact_argocd_credentials(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_argocd_credentials(item) for item in value]
    return value
