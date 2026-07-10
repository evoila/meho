# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reviewed ``sddc`` ExecutionProfile for the SDDC Manager connector.

T4 of Initiative #2271 (#2290) — the declarative source of truth the typed
:class:`~meho_backplane.connectors.sddc_manager.connector.SddcManagerConnector`
now derives its session auth from, mirroring the
:data:`~meho_backplane.connectors.vcf_logs.profile.VRLI_EXECUTION_PROFILE`
capstone (#1974).

Single source, two consumers
============================

This constant is the byte-for-byte equivalent of the shipped catalog profile
``connectors/profiles/sddc_manager_minimal.yaml`` (a parity test pins them
together). The two consumers read the *same* declarative auth block:

* the **boot-stamp** path parses the shipped YAML and — on an *unoccupied*
  ``(sddc, 9.0, sddc-rest)`` triple — synthesises a
  :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` from it;
* the **typed** :class:`SddcManagerConnector` (which *does* occupy that triple,
  so the boot-stamp no-ops on it — preserving the #1750/#1798 product-shadowing
  invariant) imports this constant and derives its session-login path,
  request headers, and session-expiry status set from the named
  ``session_login_token`` scheme (#2287).

What the profile drives on the typed connector
==============================================

Only the **auth** block is load-bearing for the typed connector: the
``session_login_token`` scheme fixes SDDC Manager's real flow —
``POST /v1/tokens`` with a ``{username, password}`` JSON body, read
``accessToken`` out of the response, send it as ``Authorization: Bearer
<accessToken>``. The appliance rejects HTTP Basic outright (live 401;
Broadcom KBs 435716/387124/372387), so the token flow is the only path that
dispatches.

The ``fingerprint`` recipe here (``/v1/releases/system`` → literal
top-level ``version``) is the *declarative* version source a profiled
connector would use; the typed connector keeps its bespoke
``elements[0]``-off-``/v1/sddc-managers`` fingerprint (nested, not
expressible as a literal top-level key), so it does not derive its
fingerprint from this profile — the same split vRLI takes. ``expiry_statuses``
defaults to ``{401}``: SDDC Manager signals an expired token with a plain
``401`` and there is no vendor-specific expiry code (the refresh-token leg is
an initiative Non-goal).
"""

from __future__ import annotations

from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)

__all__ = ["SDDC_EXECUTION_PROFILE"]


#: The reviewed declarative profile for SDDC Manager 9.x. The single source
#: of truth for the connector's auth scheme; the typed
#: :class:`SddcManagerConnector` derives its session-login path, request
#: headers, and session-expiry status set from the named
#: ``session_login_token`` scheme this profile selects. Kept in lock-step
#: with the shipped ``sddc_manager_minimal.yaml`` catalog profile by a parity
#: test.
SDDC_EXECUTION_PROFILE = ExecutionProfile(
    product="sddc",
    version="9.0",
    auth=AuthSpec(scheme="session_login_token", secret_fields=("username", "password")),
    fingerprint=FingerprintSpec(
        path="/v1/releases/system",
        authenticated=True,
        version_key="version",
        version_splitter="none",
    ),
    probe="delegate",
    pagination=PaginationSpec(strategy="none", items_key="elements"),
)
