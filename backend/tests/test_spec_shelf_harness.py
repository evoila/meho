# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit coverage for the product-agnostic spec-shelf harness (#2980).

Always-on (no shelf required): exercises the resolver's uniform
skip/None semantics against synthetic shelves in ``tmp_path``, the
OpenAPI ``METHOD:/path`` extraction through the real ingest parser,
and the reconcile assert's actionable-diff failure shape. The
shelf-gated demonstration lane lives in
``tests/test_spec_shelf_example_lane.py``; the vcenter delegation pins
at the end of this module prove the #2980 generalisation kept
:mod:`tests.acceptance._vcenter_spec` behavior-identical.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests import _spec_shelf
from tests.acceptance import _vcenter_spec

if TYPE_CHECKING:
    from pathlib import Path

_ALL_RESOLVER_ENV_VARS = (
    _spec_shelf.SHELF_ENV_VAR,
    "MEHO_VCENTER_OPENAPI_VCENTER",
    "MEHO_VCENTER_OPENAPI_VI_JSON",
    "MEHO_VCENTER_OPENAPI",
)


@pytest.fixture
def clean_resolver_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Isolate resolution from any shelf configured on the host machine."""
    for env_var in _ALL_RESOLVER_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    return monkeypatch


def _make_shelf(root: Path, product_dir: str, filename: str, body: str = "{}") -> Path:
    spec_path = root / product_dir / filename
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(body, encoding="utf-8")
    return spec_path


# ---------------------------------------------------------------------------
# resolve_shelf_file / require_shelf_spec — uniform resolution + skip
# ---------------------------------------------------------------------------


def test_resolve_returns_none_when_env_unset(clean_resolver_env: pytest.MonkeyPatch) -> None:
    assert _spec_shelf.resolve_shelf_file("nsx-9.0", "nsx-openapi.json") is None


def test_resolve_returns_none_when_env_empty(clean_resolver_env: pytest.MonkeyPatch) -> None:
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, "")
    assert _spec_shelf.resolve_shelf_file("nsx-9.0", "nsx-openapi.json") is None


def test_resolve_returns_none_when_product_dir_absent(
    clean_resolver_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A shelf without the product dir (sparse checkout not widened) resolves None."""
    _make_shelf(tmp_path, "vcenter-9.0", "vcenter.yaml")
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, str(tmp_path))
    assert _spec_shelf.resolve_shelf_file("nsx-9.0", "nsx-openapi.json") is None


def test_resolve_returns_the_pinned_file(
    clean_resolver_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec_path = _make_shelf(tmp_path, "nsx-9.0", "nsx-openapi.json")
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, str(tmp_path))
    assert _spec_shelf.resolve_shelf_file("nsx-9.0", "nsx-openapi.json") == spec_path


def test_require_shelf_spec_skips_with_the_uniform_reason(
    clean_resolver_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _spec_shelf.require_shelf_spec("nsx-9.0", "nsx-openapi.json")
    assert "nsx-9.0/nsx-openapi.json spec not configured" in str(excinfo.value)
    assert _spec_shelf.SHELF_ENV_VAR in str(excinfo.value)


# ---------------------------------------------------------------------------
# openapi_served_op_ids — parser-emitted METHOD:/path extraction
# ---------------------------------------------------------------------------


def test_openapi_served_op_ids_match_the_parser_emission(tmp_path: Path) -> None:
    """Op_ids come from the real ingest parser, action-suffix keying intact."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "demo", "version": "1.0"},
        "paths": {
            "/vcenter/vm": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"200": {"description": "ok"}}},
            },
            "/vcenter/vm/{vm}/power?action=start": {
                "post": {"responses": {"200": {"description": "ok"}}},
            },
        },
    }
    spec_path = tmp_path / "demo-openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    assert _spec_shelf.openapi_served_op_ids(spec_path) == {
        "GET:/vcenter/vm",
        "POST:/vcenter/vm",
        "POST:/vcenter/vm/{vm}/power?action=start",
    }


# ---------------------------------------------------------------------------
# assert_op_ids_served — green, actionable red, vacuous-empty guard
# ---------------------------------------------------------------------------


def test_assert_passes_when_every_declared_op_id_is_served() -> None:
    _spec_shelf.assert_op_ids_served(
        {"GET:/vcenter/vm"},
        {"GET:/vcenter/vm", "POST:/vcenter/vm"},
        spec_label="vcenter-9.0/vcenter.yaml",
    )


def test_assert_fails_with_missing_entries_and_near_misses() -> None:
    """The failure names each unserved op_id and greps served near-misses.

    Reproduces the real #2970 finding shape: the unserved
    ``distributed-switches`` path had its resource living under another
    API family in the pinned spec — the near-miss line is the repoint lead.
    """
    served = {
        "GET:/vcenter/namespace-management/networks/distributed-switches",
        "GET:/vcenter/network",
    }
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _spec_shelf.assert_op_ids_served(
            {"GET:/vcenter/network/distributed-switches", "GET:/vcenter/network"},
            served,
            spec_label="vcenter-9.0/vcenter.yaml",
        )
    message = str(excinfo.value)
    assert "1 hand-coded op_id(s) are not served" in message
    assert "GET:/vcenter/network/distributed-switches" in message
    assert "near miss: GET:/vcenter/namespace-management/networks/distributed-switches" in message
    assert "spec-reconcile-guards-standard.md" in message


def test_assert_fails_without_near_miss_when_resource_name_is_unserved() -> None:
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _spec_shelf.assert_op_ids_served(
            {"GET:/vcenter/no-such-resource"},
            {"GET:/vcenter/vm"},
            spec_label="vcenter-9.0/vcenter.yaml",
        )
    assert "near miss: none" in str(excinfo.value)


def test_assert_fails_on_empty_declared_set() -> None:
    """An empty enumeration is broken wiring, never a vacuous green."""
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _spec_shelf.assert_op_ids_served(
            set(), {"GET:/vcenter/vm"}, spec_label="vcenter-9.0/vcenter.yaml"
        )
    assert "declared no op_ids" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Vcenter delegation pins — #2980 kept _vcenter_spec behavior-identical
# ---------------------------------------------------------------------------


def test_vcenter_resolvers_delegate_to_the_generic_shelf_root(
    clean_resolver_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vcenter_yaml = _make_shelf(tmp_path, "vcenter-9.0", "vcenter.yaml")
    vi_json = _make_shelf(tmp_path / "vi", "vcenter-9.0", "vi-json.yaml")
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, str(tmp_path))
    assert _vcenter_spec.resolve_vcenter_yaml() == vcenter_yaml
    assert _vcenter_spec.resolve_vi_json_yaml() is None  # vi-json not on this shelf
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, str(tmp_path / "vi"))
    assert _vcenter_spec.resolve_vi_json_yaml() == vi_json


def test_vcenter_explicit_env_vars_still_beat_the_shelf_root(
    clean_resolver_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _make_shelf(tmp_path, "vcenter-9.0", "vcenter.yaml")
    explicit = tmp_path / "explicit-vcenter.yaml"
    explicit.write_text("{}", encoding="utf-8")
    clean_resolver_env.setenv(_spec_shelf.SHELF_ENV_VAR, str(tmp_path))
    clean_resolver_env.setenv("MEHO_VCENTER_OPENAPI_VCENTER", str(explicit))
    assert _vcenter_spec.resolve_vcenter_yaml() == explicit


def test_vcenter_resolvers_return_none_when_nothing_is_configured(
    clean_resolver_env: pytest.MonkeyPatch,
) -> None:
    assert _vcenter_spec.resolve_vcenter_yaml() is None
    assert _vcenter_spec.resolve_vi_json_yaml() is None
