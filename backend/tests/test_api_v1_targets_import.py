# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration tests for the bulk-import flow against /api/v1/targets.

The Go CLI side of this flow lives at
``cli/internal/cmd/targets/import.go`` (G0.3-T6 / Task #257). Those
tests exercise the YAML parser and the CREATE / UPDATE plan builder.
*This* module exercises the **server side** of the same flow: it
replays the per-entry POST / PATCH requests the CLI would fire and
asserts the round-trip semantics.

Coverage matrix:

* **Real-consumer YAML round-trip** — every entry in the pinned
  snapshot of
  ``https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/rdc-hetzner-dc/targets.yaml``
  (24 entries) creates a target with the right top-level columns and
  the right `extras` spill.
* **`fingerprint` is rejected on POST** — verifies the server-side
  contract the CLI's `skipSilent` path relies on (sending
  fingerprint in a CREATE body raises 422 via
  ``model_config = ConfigDict(extra='forbid')``).
* **`preferred_impl_id` is accepted as top-level on POST** —
  verifies the G0.3-T1.5 (#477) amendment is reflected in the API
  (a top-level column, not a spill into `extras`).
* **Sparse-PATCH semantics** — a PATCH containing only `notes`
  must not wipe the other columns the target already has.
  Mirrors the inverse of the bug PR #362's review on #257
  surfaced: the import tool was sending PUT-shaped bodies because
  the route handler's ``model_dump(exclude_unset=True)`` +
  ``setattr`` loop combined with Pydantic v2's "explicit null
  counts as set" rule wiped omitted columns.

The fixture file is pinned to a specific SHA (recorded in the
helper below). When the upstream `targets.yaml` evolves, regenerate
the fixture and update both the SHA constant and the entry-count
assertion below — the test is deliberately tight on shape so that a
silent drift in the upstream contract surfaces as a CI failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from fastapi.testclient import TestClient

from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

# Pinned snapshot location and provenance. The fixture content
# derives from the upstream blob at
# ``evoila-bosnia/claude-rdc-hetzner-dc:rdc-hetzner-dc/targets.yaml``
# at the SHA recorded below; one historical-leaked-and-rotated
# password string in a `notes:` field has been redacted to
# ``[REDACTED — see incident log]`` for security-scanner hygiene,
# so a byte-for-byte SHA match against upstream is intentionally
# not asserted. Regenerate together if the consumer file evolves.
# The entry-count constant below pins to the upstream SHA's
# structure; mismatches surface as a fixture-drift test failure.
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "rdc-hetzner-dc-targets.yaml"
_FIXTURE_UPSTREAM_SHA = "1e1e6885e1ef79d03001955205d9e44a0bc8e79d"
_FIXTURE_EXPECTED_ENTRY_COUNT = 24

# Mapping rules mirror the Go side at cli/internal/cmd/targets/import.go.
# Keep this in sync with `knownTopLevel` and `skipSilent` there.
_KNOWN_TOP_LEVEL = frozenset(
    {
        "aliases",
        "auth_model",
        "extras",
        "fqdn",
        "host",
        "name",
        "notes",
        "port",
        "preferred_impl_id",
        "product",
        "secret_ref",
        "vpn_required",
    }
)
_SKIP_SILENT = frozenset({"fingerprint"})


@pytest.fixture(autouse=True)
def _legacy_layout_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run this module as a mid-migration deploy (tenant-scope guard off).

    The pinned consumer fixture predates the per-tenant Vault layout
    (#1723): its ``secret_ref`` values live under the consumer's own
    ``secret/rdc-hetzner-dc/...`` subtree. On a guard-**on** deploy the
    #2091 write-time gate now rejects exactly these refs by design (that
    consumer's dispatch-time ``permission denied`` is the signal #2091
    exists to fail fast on) — the reject path is pinned in
    ``test_api_v1_targets.py``'s "#2091 secret_ref tenant-scope
    fail-fast" cluster. *This* module's claim is the CLI↔server
    bulk-import mapping round-trip, so it opts out via the documented
    mid-migration escape hatch (``VAULT_KV_TENANT_SCOPE_PREFIX=""``),
    the same state a deploy importing a legacy ``targets.yaml`` runs in.
    """
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _entry_to_create_body(entry: dict[str, Any]) -> dict[str, Any]:
    """Apply the CLI's mapping rules to one YAML entry.

    Identical contract to ``mapEntry`` in
    ``cli/internal/cmd/targets/import.go``: known keys map 1:1 to
    top-level columns; ``fingerprint`` is dropped; every other key
    spills into ``extras``. The output is exactly what the CLI POSTs
    to ``/api/v1/targets`` for a CREATE.
    """
    body: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    # Pre-extract any explicit `extras:` block.
    if isinstance(entry.get("extras"), dict):
        extras.update(entry["extras"])
    for k, v in entry.items():
        if k == "extras":
            continue
        if k in _SKIP_SILENT:
            continue
        if k in _KNOWN_TOP_LEVEL:
            body[k] = v
        else:
            extras[k] = v
    if extras:
        body["extras"] = extras
    return body


def test_fixture_parses_and_has_expected_entry_count() -> None:
    """The pinned fixture matches the upstream entry count."""
    with _FIXTURE_PATH.open("rb") as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict)
    assert "targets" in doc
    assert len(doc["targets"]) == _FIXTURE_EXPECTED_ENTRY_COUNT, (
        f"Pinned snapshot drift: fixture has {len(doc['targets'])} entries; "
        f"expected {_FIXTURE_EXPECTED_ENTRY_COUNT} at upstream SHA "
        f"{_FIXTURE_UPSTREAM_SHA}. Regenerate the fixture + update the "
        "constant if the upstream contract intentionally moved."
    )


def test_real_targets_yaml_full_roundtrip(client: TestClient) -> None:
    """Every conformant entry in the consumer's targets.yaml creates a Target.

    Replays the per-entry POST the CLI would fire. Asserts:

    * Every conformant entry returns 201.
    * Top-level columns recognised by the API land at the top level.
    * Unknown YAML fields (e.g. ``sso_realm``, ``kubeconfig_field``,
      ``account``, ``project_id``) spill into ``extras``.
    * ``notes`` survives multi-line YAML scalars verbatim.

    Entries missing the required ``host`` field are filtered out
    here — the consumer's real file legitimately omits ``host`` for
    a cloud-provider target (``rdc-gcp``, accessed by project ID +
    account rather than hostname). The Go CLI parser at
    ``cli/internal/cmd/targets/import.go`` would reject such a file
    before firing the first HTTP request (its ``parseTargetsYAML``
    requires ``name``, ``product``, ``host`` on every entry), and a
    matching Go unit test
    (``TestParseTargetsYAMLMissingRequiredFails``) pins that
    behaviour. The expectation for the consumer is that they fix
    these entries — add a synthetic ``host: cloud-provider`` line
    or split cloud-provider targets into a separate file — before
    running ``meho targets import``.

    This is the acceptance-criteria-9 gate from the issue body: "CI
    integration test against the consumer's real targets.yaml (or a
    snapshot of it pinned to a specific git SHA)". The "pinned
    snapshot" arm is what's used here; the constants at the top of
    this module track which upstream SHA the snapshot reflects.
    """
    with _FIXTURE_PATH.open("rb") as f:
        doc = yaml.safe_load(f)
    all_entries = doc["targets"]
    # Filter out entries that omit a required field. See docstring
    # above for why this is acceptable — the CLI rejects them; the
    # API integration scope is "the entries that would pass".
    entries = [
        e
        for e in all_entries
        if isinstance(e.get("name"), str)
        and isinstance(e.get("product"), str)
        and isinstance(e.get("host"), str)
    ]
    # We must still cover most of the fixture — if the filter starts
    # dropping most of the file, the round-trip claim is hollow.
    assert len(entries) >= 20, (
        f"filter kept only {len(entries)}/{len(all_entries)} entries — "
        "the upstream consumer file may have drifted in shape"
    )

    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        for entry in entries:
            body = _entry_to_create_body(entry)
            r = client.post("/api/v1/targets", json=body, headers=headers)
            assert r.status_code == 201, (
                f"entry {entry.get('name')!r} failed: HTTP {r.status_code} {r.text}"
            )
            data = r.json()
            # Top-level columns mirror the YAML.
            assert data["name"] == entry["name"]
            assert data["product"] == entry["product"]
            assert data["host"] == entry["host"]
            # `notes` round-trips verbatim (multi-line scalars are
            # the failure mode the issue body explicitly calls out).
            if "notes" in entry:
                assert data["notes"] == entry["notes"]
            # Unknown YAML fields land in extras.
            unknown_keys = set(entry.keys()) - _KNOWN_TOP_LEVEL - _SKIP_SILENT
            for k in unknown_keys:
                assert k in data["extras"], (
                    f"entry {entry['name']!r}: expected unknown key {k!r} in extras, "
                    f"got extras={data['extras']!r}"
                )
                assert data["extras"][k] == entry[k]


def test_create_rejects_fingerprint_via_extra_forbid(client: TestClient) -> None:
    """POST with ``fingerprint`` returns 422 (server-managed column).

    Verifies the contract the CLI's ``skipSilent`` set relies on: if
    a YAML entry slipped a ``fingerprint`` key past the CLI's filter,
    the server would reject the entire POST. Skipping at the CLI
    side is the operator-friendly path.
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-vault",
                "product": "vault",
                "host": "vault.evba.lab",
                # Pydantic v2 ConfigDict(extra='forbid') rejects this.
                "fingerprint": {"vendor": "hashicorp", "version": "1.15.0"},
            },
            headers=headers,
        )
    assert r.status_code == 422
    detail = r.json()["detail"]
    # Pydantic v2 surfaces the offending field name in detail[*].loc.
    assert any("fingerprint" in tuple(map(str, item.get("loc", []))) for item in detail)


def test_create_accepts_preferred_impl_id_top_level(client: TestClient) -> None:
    """POST with ``preferred_impl_id`` lands as a top-level column.

    Verifies the G0.3-T1.5 (#477) amendment: ``preferred_impl_id`` is
    a real top-level field on TargetCreate, not an extras spill. The
    CLI's mapping rule depends on this — sending it inside ``extras``
    would silently lose the operator's override.
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r = client.post(
            "/api/v1/targets",
            json={
                "name": "vault-tie-break",
                "product": "vault",
                "host": "vault.evba.lab",
                "preferred_impl_id": "vault-1.x",
            },
            headers=headers,
        )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["preferred_impl_id"] == "vault-1.x"
    # And it didn't accidentally also land in extras.
    assert data["extras"] == {}


def test_sparse_patch_preserves_omitted_columns(client: TestClient) -> None:
    """A PATCH with only ``notes`` does not wipe other columns.

    This is the inverse of the bug PR #362's review on #257
    surfaced: the import tool sent every column on every UPDATE,
    so re-running ``--update`` against a YAML that omitted some
    fields would reset those columns to defaults on every run. The
    fix (sparse PATCH body — see ``entryToUpdateBody`` in
    cli/internal/cmd/targets/import.go) depends on the server
    behaving correctly when given a 1-key PATCH body. This test
    pins that behaviour.
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Seed a rich target.
        r = client.post(
            "/api/v1/targets",
            json={
                "name": "rdc-rich",
                "product": "vault",
                "host": "vault.evba.lab",
                "aliases": ["v1", "vault-main"],
                "port": 8200,
                "vpn_required": True,
                "auth_model": "shared_service_account",
                "extras": {"sso_realm": "evba.lab"},
                "notes": "original",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text

        # Sparse PATCH — only `notes`.
        r2 = client.patch(
            "/api/v1/targets/rdc-rich",
            json={"notes": "updated"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        data = r2.json()
        # The patched column changed.
        assert data["notes"] == "updated"
        # Everything else survived.
        assert tuple(data["aliases"]) == ("v1", "vault-main")
        assert data["port"] == 8200
        assert data["vpn_required"] is True
        assert data["auth_model"] == "shared_service_account"
        assert data["extras"] == {"sso_realm": "evba.lab"}


def test_duplicate_name_aborts_default_mode_workflow(client: TestClient) -> None:
    """Re-running a CREATE for an existing name returns 409.

    Verifies the contract the CLI's default-mode (no ``--update``)
    duplicate-detection branch relies on. The CLI builds the plan,
    sees the duplicates against the list-targets response, and
    aborts before firing any write — but the server-side gate is
    the load-bearing one for safety.
    """
    key = make_rsa_keypair("kid-A")
    tenant_id = DEFAULT_TENANT_ID
    headers = {"Authorization": f"Bearer {_admin_token(key, tenant_id)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        r1 = client.post(
            "/api/v1/targets",
            json={"name": "rdc-vcenter", "product": "vcenter", "host": "vc.evba.lab"},
            headers=headers,
        )
        r2 = client.post(
            "/api/v1/targets",
            json={"name": "rdc-vcenter", "product": "vcenter", "host": "vc.evba.lab"},
            headers=headers,
        )
    assert r1.status_code == 201
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert "rdc-vcenter" in detail


def test_list_targets_after_import_returns_all_names(client: TestClient) -> None:
    """``GET /api/v1/targets`` lists every imported entry.

    Pins the contract the CLI's ``buildLivePlan`` relies on: after
    importing a batch, the list endpoint returns the same names so a
    subsequent ``--dry-run`` against the same YAML correctly
    classifies every entry as UPDATE (rather than re-CREATE).
    """
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        # Import a small batch synthesised from the fixture.
        with _FIXTURE_PATH.open("rb") as f:
            doc = yaml.safe_load(f)
        first_three = doc["targets"][:3]
        for entry in first_three:
            body = _entry_to_create_body(entry)
            r = client.post("/api/v1/targets", json=body, headers=headers)
            assert r.status_code == 201, r.text
        # List back.
        r2 = client.get(
            "/api/v1/targets",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
        assert r2.status_code == 200
        listed_names = {t["name"] for t in r2.json()}
        for entry in first_three:
            assert entry["name"] in listed_names


@pytest.fixture(autouse=True)
def _seed_tenant_for_admin() -> Iterator[None]:
    """Ensure the test tenant exists before each test.

    The targets routes are tenant-scoped via the JWT's
    ``tenant_id`` claim; the tests above use the default tenant via
    ``_admin_token`` / ``_operator_token``. The autouse fixture is a
    no-op today (the test DB seeds tenants on import) but kept here
    so future schema changes that gate tenant pre-existence have a
    visible seam.
    """
    yield
