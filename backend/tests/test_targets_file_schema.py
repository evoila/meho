# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The target record schema file, and a toolkit targets file against it.

``docs/schemas/target-record-v1.schema.json`` describes one record of the
toolkit's ``targets.yaml``. These tests check three things:

* the checked-in file matches the model;
* the keys of the schema match the keys ``meho targets import`` and the
  console import map to a column;
* the schema refuses what the target write model refuses, and accepts
  what it accepts;
* a toolkit file imports into a tenant without edits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from scripts.generate_targets_file_schema import SCHEMA_PATH, render_schema

from meho_backplane.settings import get_settings
from meho_backplane.targets.file_schema import TargetRecord
from meho_backplane.targets.schemas import TargetCreate
from meho_backplane.ui.routes.connectors.import_view import (
    _KNOWN_TOP_LEVEL as CONSOLE_KNOWN_KEYS,
)
from meho_backplane.ui.routes.connectors.import_view import (
    _SKIP_SILENT as CONSOLE_SKIPPED_KEYS,
)
from meho_backplane.ui.routes.connectors.import_view import (
    _map_entry,
)

from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from .test_targets_schemas import _VALID_CA_PEM

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = Path(__file__).parent / "fixtures" / "toolkit-targets-v1.yaml"
_GO_IMPORT = _REPO_ROOT / "cli" / "internal" / "cmd" / "targets" / "import.go"

#: The smallest record both the schema and the write model accept.
_MINIMAL = {"name": "x", "product": "linux", "host": "h.example.com"}


@pytest.fixture(autouse=True)
def _legacy_layout_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture's secret paths are toolkit paths, not per-tenant ones."""
    monkeypatch.setenv("VAULT_KV_TENANT_SCOPE_PREFIX", "")
    get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _go_key_set(name: str) -> set[str]:
    """The quoted keys of a ``map[string]struct{}`` named *name* in import.go."""
    text = _GO_IMPORT.read_text(encoding="utf-8")
    match = re.search(rf"var {name} = map\[string\]struct\{{\}}\{{(.*?)\n\}}", text, re.DOTALL)
    assert match, f"{name} not found in import.go"
    return set(re.findall(r'"([a-z_]+)":', match.group(1)))


def _fixture_entries() -> list[dict[str, Any]]:
    doc = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    return list(doc["targets"])


def test_schema_file_matches_the_model() -> None:
    on_disk = (_REPO_ROOT / SCHEMA_PATH).read_text(encoding="utf-8")
    assert on_disk == render_schema(), (
        "docs/schemas/target-record-v1.schema.json is out of date. Run "
        "`cd backend && uv run python scripts/generate_targets_file_schema.py`."
    )


def test_schema_keys_match_the_import_keys() -> None:
    schema_keys = set(TargetRecord.model_fields)
    assert schema_keys == _go_key_set("knownTopLevel") | _go_key_set("skipSilent")
    assert schema_keys == CONSOLE_KNOWN_KEYS | CONSOLE_SKIPPED_KEYS


def _schema_errors(entry: dict[str, Any]) -> list[str]:
    schema = json.loads((_REPO_ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    return [e.message for e in Draft202012Validator(schema).iter_errors(entry)]


def test_toolkit_file_is_valid_against_the_schema_and_the_write_model() -> None:
    for entry in _fixture_entries():
        assert _schema_errors(entry) == [], entry["name"]
        body, _ = _map_entry(entry)
        TargetCreate.model_validate(body)


@pytest.mark.parametrize(
    "bad_entry",
    [
        {**_MINIMAL, "name": "Example Server"},
        {**_MINIMAL, "name": "lab/vc"},
        {**_MINIMAL, "name": "lab\tvc"},
        {**_MINIMAL, "aliases": [""]},
        {**_MINIMAL, "aliases": ["vc01", "lab vc"]},
        {**_MINIMAL, "version": "9" * 101},
        {**_MINIMAL, "preferred_impl_id": "p" * 201},
        {**_MINIMAL, "secret_field_map": {"": "login"}},
        {**_MINIMAL, "secret_field_map": {f"field_{i}": "login" for i in range(51)}},
        {**_MINIMAL, "tls_ca_pin": _VALID_CA_PEM, "verify_tls": False},
    ],
)
def test_schema_refuses_what_the_write_model_refuses(bad_entry: dict[str, Any]) -> None:
    assert _schema_errors(bad_entry) != []
    body, _ = _map_entry(bad_entry)
    with pytest.raises(ValidationError):
        TargetCreate.model_validate(body)


@pytest.mark.parametrize(
    "entry",
    [
        {**_MINIMAL, "name": "lab-vc.01", "aliases": ["vc01", "vcenter_lab"]},
        {**_MINIMAL, "version": None},
        {**_MINIMAL, "host": "db-01.example.com:27017"},
        {**_MINIMAL, "tls_ca_pin": _VALID_CA_PEM},
        {**_MINIMAL, "tls_ca_pin": _VALID_CA_PEM, "verify_tls": True},
        {**_MINIMAL, "secret_field_map": {f"field_{i}": "login" for i in range(50)}},
    ],
)
def test_schema_accepts_what_the_write_model_accepts(entry: dict[str, Any]) -> None:
    assert _schema_errors(entry) == []
    body, _ = _map_entry(entry)
    TargetCreate.model_validate(body)


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"product": "linux", "host": "h.example.com"},
        {"name": "x", "host": "h.example.com"},
        {"name": "x", "product": "linux"},
        {"name": "x", "product": "linux", "host": "h.example.com", "version": 8.0},
        {"name": "x", "product": "linux", "host": "h.example.com", "port": 70000},
        {"name": "x", "product": "linux", "host": "h.example.com", "auth_model": "other"},
        {"name": "x", "product": "linux", "host": "h.example.com", "fingerprint": {"vendor": "v"}},
    ],
)
def test_schema_rejects_a_bad_record(bad_entry: dict[str, Any]) -> None:
    assert _schema_errors(bad_entry) != []


def test_console_mapping_keeps_version_and_tls_keys_as_columns() -> None:
    entry = _fixture_entries()[2]
    body, warnings = _map_entry(entry)
    assert body["version"] == "1.31"
    assert body["verify_tls"] is False
    assert "version" not in body.get("extras", {})
    assert warnings == []


def test_console_mapping_skips_fingerprint_with_a_warning() -> None:
    body, warnings = _map_entry(_fixture_entries()[0])
    assert "fingerprint" not in body
    assert "fingerprint" not in body["extras"]
    assert body["extras"] == {"sso_realm": "lab.example.com"}
    assert len(warnings) == 1


def test_toolkit_file_imports_into_a_tenant_without_edits(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    headers = {"Authorization": f"Bearer {_admin_token(key)}"}
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        for entry in _fixture_entries():
            body, _ = _map_entry(entry)
            r = client.post("/api/v1/targets", json=body, headers=headers)
            assert r.status_code == 201, f"{entry['name']}: {r.status_code} {r.text}"
            data = r.json()
            assert data["name"] == entry["name"]
            assert data["product"] == entry["product"]
            assert data["version"] == entry.get("version")
            assert data["host"] == entry["host"]
            # The import never writes the detected product and version.
            assert not data.get("fingerprint")
        listed = client.get("/api/v1/targets", headers=headers).json()
    names = {t["name"] for t in listed["items"]}
    assert names == {e["name"] for e in _fixture_entries()}
