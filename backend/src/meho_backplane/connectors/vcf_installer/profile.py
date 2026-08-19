# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reviewed ``installer`` ExecutionProfile for the VCF Installer connector.

The declarative source of truth the typed
:class:`~meho_backplane.connectors.vcf_installer.connector.InstallerConnector`
derives its session auth from — the same posture the sddc-manager connector
takes against ``SDDC_EXECUTION_PROFILE``.

Auth
====

The Installer's real flow is the ``session_login_token`` scheme (#2287), byte-
for-byte the SDDC Manager token flow: ``POST /v1/tokens`` with a
``{username, password}`` body (``TokenCreationSpec``), read ``accessToken`` out
of the ``TokenPair`` response, send it as ``Authorization: Bearer <accessToken>``
on every subsequent request. Reusing the named scheme means the login mechanics
are declared once and cannot drift.

Fingerprint
===========

``GET /v1/system/appliance-info`` returns ``ApplianceInfo`` with a literal
top-level ``version`` string (and ``role``) — the Installer appliance's own
version, served pre-bring-up (unlike a deployed SDDC Manager, an Installer has
no inventory to fingerprint from). ``expiry_statuses`` defaults to ``{401}``: the
Installer signals an expired token with a plain ``401`` (no vendor-specific
expiry code; the refresh-token leg is a Non-goal here).
"""

from __future__ import annotations

from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)

__all__ = ["INSTALLER_EXECUTION_PROFILE"]


#: The reviewed declarative profile for VCF Installer 9.1. The single source of
#: truth for the connector's auth scheme; the typed
#: :class:`InstallerConnector` derives its session-login path, request headers,
#: and session-expiry status set from the named ``session_login_token`` scheme
#: this profile selects.
INSTALLER_EXECUTION_PROFILE = ExecutionProfile(
    product="installer",
    version="9.1",
    auth=AuthSpec(scheme="session_login_token", secret_fields=("username", "password")),
    fingerprint=FingerprintSpec(
        path="/v1/system/appliance-info",
        authenticated=True,
        version_key="version",
        version_splitter="none",
    ),
    probe="delegate",
    pagination=PaginationSpec(strategy="none", items_key="elements"),
)
