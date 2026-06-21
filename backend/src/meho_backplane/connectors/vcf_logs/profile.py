# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The reviewed ``vrli_session`` ExecutionProfile for the vRLI connector.

G0.28-T8 (#1974) — the **capstone** of Initiative #1965: the first real
:class:`~meho_backplane.connectors.profile.ExecutionProfile` authored against
a shipped, bespoke connector, proving the whole profiled-connector chain
(T1-T7) works end to end on production code rather than synthetic fixtures.

What this profile retires from the typed :class:`VcfLogsConnector`
=================================================================

vRLI's auth + fingerprint were the two hand-coded surfaces a declarative
profile can express, so they move here, with the profile as the single
source of truth the typed connector now derives from (no duplicated
literals):

* **auth** — the ``session_login`` named scheme (#1970): POST
  ``username`` / ``password`` to ``/api/v2/sessions``, read ``sessionId``
  out of the JSON body, send it as ``Authorization: Bearer <sessionId>``.
  The per-target lock / token cache / single-flight / re-login-once
  harness is the one hoisted into
  :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector`.
* **fingerprint** — the unauthenticated ``GET /api/v2/version`` recipe
  (#1972): read the literal top-level ``version`` key and render it via
  the ``vrli_five_part`` named splitter (``"9.0.0.0.21761695"`` →
  ``("9.0.0", "21761695")``).
* **expiry_statuses** — vRLI's ``{401, 440}`` (#1973): ``401`` is the
  session-expiry floor every session connector recovers from; ``440`` is
  vRLI's own ``trait.authenticated.440`` ("the session ID has expired;
  obtain a new session ID") emitted once the appliance idle-times out the
  in-memory session (the case that bit live consumers on v0.17.0, #1909).

What stays bespoke (the profile cannot model it)
================================================

The ``session_login`` scheme's login body is a fixed
``{username, password, provider="Local"}`` — the profile carries no
per-target ``provider`` knob (``ActiveDirectory`` / ``vIDM``), and the
declarative fingerprint emits only ``(version, build)`` with no
``extras["release_name" | "version_full" | "patch"]``. Both stay typed-only
enrichment on :class:`VcfLogsConnector`, layered on top of the profile-driven
core (see that class's docstring). The ``ResultHandle`` large-result path is
the connector-agnostic JSONFlux dispatch mechanism
(:class:`~meho_backplane.connectors.schemas.ResultHandle` +
:class:`~meho_backplane.operations.jsonflux_reducer.JsonFluxReducer`), not
connector code, and is untouched by this migration.

The profile is a module constant, not a stored row: the persistence /
review-gate stamping path (T5 #1971) makes a *profiled* connector
dispatchable behind the ``is_enabled`` / ``review_status`` interlock; this
pilot proves parity against the typed connector, so the constant is the
single declarative source both the typed connector and the parity test
read from.
"""

from __future__ import annotations

from meho_backplane.connectors.profile import (
    AuthSpec,
    ExecutionProfile,
    FingerprintSpec,
    PaginationSpec,
)

__all__ = ["VRLI_EXECUTION_PROFILE"]


#: The reviewed declarative profile for vRLI 9.x. The single source of
#: truth for the connector's auth scheme, fingerprint recipe, and
#: session-expiry status set; the typed :class:`VcfLogsConnector` derives
#: those from this constant, and the integration parity test stamps it onto
#: a :class:`~meho_backplane.connectors.profiled.ProfiledRestConnector` to
#: prove per-method dispatch parity. ``pagination`` is ``none`` — vRLI's
#: curated read ops are constraint-path single-shot calls whose large
#: result sets flow through the JSONFlux reducer, not a paginated list loop.
VRLI_EXECUTION_PROFILE = ExecutionProfile(
    product="vrli",
    version="9.0",
    auth=AuthSpec(scheme="session_login", secret_fields=("username", "password")),
    fingerprint=FingerprintSpec(
        path="/api/v2/version",
        authenticated=False,
        version_key="version",
        version_splitter="vrli_five_part",
    ),
    probe="delegate",
    pagination=PaginationSpec(strategy="none", items_key="events"),
    expiry_statuses=frozenset({401, 440}),
)
