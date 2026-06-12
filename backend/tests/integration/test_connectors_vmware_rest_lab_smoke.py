# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""G3.9-T3 — opt-in live lab-vCenter smoke for the vmware-rest credential read.

The **live** counterpart to the recorded-fixture E2E
(``tests/test_connectors_vmware_rest_credread.py``). Where that replays a
canned Vault read + a respx-mocked vCenter in the secret-free unit lane,
this test runs the *real* chain once against a real lab vCenter, reading
the real service-account credential out of a real Vault under a real
operator JWT — the rubric **State 2** proof against live infrastructure.

It is **opt-in and skips cleanly by default** so secret-free CI stays
green. The gate is the presence of every required env var; with any one
unset, the module collects and the test reports ``skipped`` (verified by
running the suite with the vars unset — the default everywhere except a
deliberate operator-run lab smoke).

Required env vars (all must be set for the test to run)
======================================================

* ``MEHO_LAB_VCENTER`` — the lab vCenter host (``vc.lab.example``); the
  target ``host`` and the respx-free base URL.
* ``MEHO_LAB_VCENTER_SECRET_REF`` — the Vault KV-v2 path holding the
  service account's ``{username, password}`` (the target ``secret_ref``).
* ``MEHO_LAB_OPERATOR_JWT`` — a real operator Keycloak JWT, forwarded to
  Vault's JWT/OIDC method (``operator.raw_jwt``). Never logged or echoed.

Why this drives ``auth_headers`` + ``_get_json`` (not ``fingerprint`` or full ``dispatch``)
==========================================================================================

The smoke proves the load-bearing leaf: *the default loader reads the
real Vault credential, under the operator's identity, and establishes a
real vCenter session.* The test passes a **real operator** (carrying the
lab JWT) to :meth:`VmwareRestConnector.auth_headers`, which runs
``_session_token`` → the default ``load_session_credentials_from_vault``
(live operator-context Vault read) → ``POST /api/session`` (HTTP basic
with the read creds) → a cached ``vmware-api-session-id`` token. It then
issues one read (``GET /api/about`` via ``_get_json`` with the same
operator) and asserts the response.

It deliberately does **not** use ``fingerprint`` / ``probe``: those
synthesise a *system* operator with an empty ``raw_jwt`` (the probe path
is system-initiated), which the live loader fail-closes on by design — so
they cannot exercise the operator-context read. A full ``dispatch`` would
additionally need a seeded ``endpoint_descriptor`` row in the lab DB;
``auth_headers`` + ``_get_json`` is the minimal real-infra path that
proves State 2 under a real operator. The recorded-fixture E2E already
proves the full ``dispatch`` path.

The credential is read server-side and never returned; this test asserts
session establishment + a reachable read response, never a credential
value.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest import VmwareRestConnector

# ---------------------------------------------------------------------------
# Opt-in gate — every required var must be present, else skip the module.
# ---------------------------------------------------------------------------

_LAB_VCENTER = os.environ.get("MEHO_LAB_VCENTER")
_LAB_SECRET_REF = os.environ.get("MEHO_LAB_VCENTER_SECRET_REF")
_LAB_OPERATOR_JWT = os.environ.get("MEHO_LAB_OPERATOR_JWT")

_LAB_READY = bool(_LAB_VCENTER and _LAB_SECRET_REF and _LAB_OPERATOR_JWT)
_SKIP_REASON = (
    "lab-vCenter smoke is opt-in: set MEHO_LAB_VCENTER, "
    "MEHO_LAB_VCENTER_SECRET_REF, and MEHO_LAB_OPERATOR_JWT to run it "
    "against real infra (skipped in secret-free CI)."
)

pytestmark = pytest.mark.skipif(not _LAB_READY, reason=_SKIP_REASON)


class _LabTarget:
    """A lab vCenter target satisfying ``VsphereTargetLike``."""

    def __init__(self) -> None:
        # Tenant-unique cache key components (#1642/#1672); without them
        # ``target_cache_key`` raises AttributeError at runtime.
        self.id: UUID = UUID(int=0x5A)
        self.tenant_id: UUID = UUID(int=0)
        self.name = "vcenter-lab-smoke"
        # Strip any scheme an operator may have included in the env var.
        self.host = (_LAB_VCENTER or "").removeprefix("https://").removeprefix("http://")
        self.port: int | None = 443
        self.secret_ref = _LAB_SECRET_REF or ""
        self.auth_model = "shared_service_account"


def _lab_operator() -> Operator:
    """Operator carrying the real lab JWT forwarded to Vault."""
    return Operator(
        sub="op-lab-smoke",
        name=None,
        email=None,
        raw_jwt=_LAB_OPERATOR_JWT or "",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def test_lab_vcenter_credread_session_and_read_under_operator() -> None:
    """The default loader reads the real Vault creds (operator-context) and reads vCenter.

    Runs the live chain once under a real operator JWT: default
    ``load_session_credentials_from_vault`` → operator-context Vault read
    → ``POST /api/session`` → cached session token → ``GET /api/about``.
    Asserts the session header is minted + the read returns a vCenter
    about payload; never asserts on a credential value.
    """
    connector = VmwareRestConnector()  # default (live) loader — no injection
    target = _LabTarget()
    operator = _lab_operator()
    try:
        # Establish the session under the operator's identity. This is the
        # State-2 leaf: the live Vault read + the vCenter login.
        headers = await connector.auth_headers(target, operator)
        assert "vmware-api-session-id" in headers
        assert headers["vmware-api-session-id"]

        # One real read over the established session.
        about = await connector._get_json(target, "/api/about", operator=operator)
    finally:
        await connector.aclose()

    assert isinstance(about, dict)
    # GET /api/about on a real vCenter carries a product_line_id (vpx for
    # vCenter); presence is enough for the smoke — the recorded-fixture
    # E2E covers the full field mapping.
    assert about, f"GET /api/about returned an empty payload: {about!r}"
