# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector package brings its own catalog rows.

The ``__init__`` of a package's connector folder calls the kit's
``register_connector_catalog`` with the name of a YAML file in that folder.
MEHO reads the file with its own ``catalog.yaml`` when it loads the
catalog. These tests check that:

* the package rows are in the catalog next to the built-in rows, and only
  while the package is registered;
* the safety overlay of a package row raises an ingested read the same way
  a built-in overlay does;
* the shipped spec and profile of a package row are read from the package's
  folder, by the boot-time check and by the catalog-driven ingest route;
* a wrong file, a missing file or a row that MEHO already has stops the
  load with an error that names the package.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.api.v1.connectors_ingest import _catalog_entry_specs
from meho_backplane.operations.ingest import catalog as catalog_module
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.catalog import (
    CatalogError,
    load_catalog,
    load_entry_profile,
    load_entry_spec,
    register_connector_catalog,
    safety_overlay_for,
    validate_shipped_artifacts,
)
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor, has_safety_floor

_FOLDER = "acme_widget"
_PACKAGE = f"meho_backplane.connectors.{_FOLDER}"
_PRODUCT = "acme"
_VERSION = "1.x"
_IMPL_ID = "acme-rest"

_INIT = (
    "from meho_connector_kit import register_connector_catalog\n"
    "\n"
    "register_connector_catalog(__name__)\n"
)

_CATALOG = f"""
entries:
  - product: {_PRODUCT}
    version: "{_VERSION}"
    impl_id: {_IMPL_ID}
    requires_connector_class: ProfiledRestConnector_acme_1_x
    upstream: null
    spec_resource: acme.spec.yaml
    profile_resource: acme.profile.yaml
    catalog_ingest: supported
    safety_overlay:
      - method: GET
        path: /v1/credentials
        safety_level: caution
        requires_approval: true
"""

_SPEC = """
openapi: "3.1.0"
info: {title: acme widget API, version: "1.0.0"}
paths:
  /v1/credentials:
    get:
      operationId: listCredentials
      responses: {"200": {description: ok}}
  /v1/widgets:
    get:
      operationId: listWidgets
      responses: {"200": {description: ok}}
"""

_PROFILE = """
product: acme
version: "1.x"
auth:
  scheme: basic
  secret_fields: [username, password]
fingerprint:
  path: /v1/widgets
  authenticated: true
  version_key: version
  version_splitter: none
probe: delegate
pagination:
  strategy: none
  items_key: widgets
"""


@pytest.fixture(autouse=True)
def _own_package_catalogs() -> Iterator[None]:
    """Start with no package catalogs; put the process's own back after."""
    before = dict(catalog_module._PACKAGE_CATALOGS)
    modules_before = set(sys.modules)
    catalog_module._PACKAGE_CATALOGS.clear()
    load_catalog.cache_clear()
    yield
    catalog_module._PACKAGE_CATALOGS.clear()
    catalog_module._PACKAGE_CATALOGS.update(before)
    load_catalog.cache_clear()
    for name in set(sys.modules) - modules_before:
        if name.startswith("meho_backplane.connectors."):
            del sys.modules[name]


def _write_package(
    root: Path,
    folder: str = _FOLDER,
    *,
    init: str = _INIT,
    files: dict[str, str] | None = None,
) -> Path:
    """Write a made-up connector folder with its catalog file and data files."""
    path = root / folder
    path.mkdir(parents=True)
    (path / "__init__.py").write_text(init, encoding="utf-8")
    for name, text in (
        files
        if files is not None
        else {"catalog.yaml": _CATALOG, "acme.spec.yaml": _SPEC, "acme.profile.yaml": _PROFILE}
    ).items():
        (path / name).write_text(text, encoding="utf-8")
    return path


def _import_package(path: Path) -> None:
    """Import *path* as ``meho_backplane.connectors.<folder>``, as the registry walk would."""
    name = f"meho_backplane.connectors.{path.name}"
    spec = importlib.util.spec_from_file_location(
        name, path / "__init__.py", submodule_search_locations=[str(path)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


@pytest.fixture
def acme_package(tmp_path: Path) -> Path:
    """The made-up package, imported: its ``__init__`` registered its catalog file."""
    path = _write_package(tmp_path)
    _import_package(path)
    return path


# -- the rows are in the catalog ---------------------------------------------


def test_without_the_package_the_row_is_not_in_the_catalog() -> None:
    assert load_catalog().get(_PRODUCT, _VERSION) is None


def test_package_rows_are_in_the_catalog_next_to_the_built_in_rows(acme_package: Path) -> None:
    """The row is there, says which package brought it, and the built-in rows stay."""
    catalog = load_catalog()
    entry = catalog.get(_PRODUCT, _VERSION)
    assert entry is not None
    assert entry.source_package == _PACKAGE
    assert entry.impl_id == _IMPL_ID
    builtin = catalog.get("harbor", "2.x")
    assert builtin is not None
    assert builtin.source_package is None
    assert "source_package" not in entry.model_dump()


def test_register_again_reads_the_file_again(acme_package: Path) -> None:
    """A second call after the first load clears the cached catalog."""
    assert load_catalog().get(_PRODUCT, _VERSION) is not None
    (acme_package / "catalog.yaml").write_text(
        _CATALOG.replace('version: "1.x"', 'version: "2.x"'), encoding="utf-8"
    )
    register_connector_catalog(_PACKAGE)
    assert load_catalog().get(_PRODUCT, "2.x") is not None
    assert load_catalog().get(_PRODUCT, _VERSION) is None


# -- the safety overlay of a package row ---------------------------------------


def test_package_overlay_raises_the_listed_read_to_caution_with_approval(
    acme_package: Path,
) -> None:
    """An ingest of the vendor file gives the listed read the level of the overlay."""
    rows = {row.path: row for row in parse_openapi("file:///acme.yaml", content=_SPEC)}
    listed = rows["/v1/credentials"]
    assert listed.safety_level == "safe", "the parser alone must not raise a GET"
    floored = apply_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=listed)
    assert floored.safety_level == "caution"
    assert floored.requires_approval is True
    assert has_safety_floor(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=listed)

    plain = apply_safety_floor(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, proto=rows["/v1/widgets"]
    )
    assert plain.safety_level == "safe"
    assert plain.requires_approval is False


def test_overlay_is_empty_without_the_package() -> None:
    assert safety_overlay_for(_PRODUCT, _VERSION) == ()


# -- the shipped spec and profile of a package row -----------------------------


def test_spec_and_profile_of_a_package_row_are_read_from_its_folder(
    acme_package: Path,
) -> None:
    catalog = load_catalog()
    entry = catalog.get(_PRODUCT, _VERSION)
    assert entry is not None
    assert "/v1/credentials" in load_entry_spec(entry)
    assert "scheme: basic" in load_entry_profile(entry)
    validate_shipped_artifacts(catalog)

    fixture = catalog.get("_fixture", "1.0")
    assert fixture is not None, "the built-in fixture row still reads from MEHO's own packages"
    assert "/things" in load_entry_spec(fixture)
    assert "scheme: basic" in load_entry_profile(fixture)


def test_catalog_ingest_route_uses_the_package_spec(acme_package: Path) -> None:
    entry = load_catalog().get(_PRODUCT, _VERSION)
    assert entry is not None
    sources = _catalog_entry_specs(entry)
    assert len(sources) == 1
    assert sources[0].uri == "spec:acme.spec.yaml"
    assert sources[0].content is not None
    assert "/v1/credentials" in sources[0].content


def test_missing_spec_file_in_the_package_names_the_package(tmp_path: Path) -> None:
    path = _write_package(tmp_path, files={"catalog.yaml": _CATALOG, "acme.profile.yaml": _PROFILE})
    _import_package(path)
    with pytest.raises(CatalogError, match=f"not found under '{re.escape(_PACKAGE)}'"):
        validate_shipped_artifacts()


def test_malformed_profile_in_the_package_names_the_package(tmp_path: Path) -> None:
    bad = _PROFILE.replace("scheme: basic", "scheme: not_a_real_scheme")
    path = _write_package(
        tmp_path,
        files={"catalog.yaml": _CATALOG, "acme.spec.yaml": _SPEC, "acme.profile.yaml": bad},
    )
    _import_package(path)
    with pytest.raises(CatalogError, match="failed dry-run parse") as excinfo:
        validate_shipped_artifacts()
    assert _PACKAGE in str(excinfo.value)


# -- a wrong file stops the load, and names the package ------------------------


def test_missing_catalog_file_stops_the_load_and_names_the_package(tmp_path: Path) -> None:
    path = _write_package(tmp_path, files={})
    _import_package(path)
    with pytest.raises(
        CatalogError, match=f"'{re.escape(_PACKAGE)}' names the catalog file 'catalog\\.yaml'"
    ):
        load_catalog()


def test_malformed_catalog_file_stops_the_load_and_names_the_package(tmp_path: Path) -> None:
    path = _write_package(tmp_path, files={"catalog.yaml": "entries: [{product: acme}]\n"})
    _import_package(path)
    with pytest.raises(
        CatalogError, match=f"'catalog\\.yaml' of the connector package '{re.escape(_PACKAGE)}'"
    ):
        load_catalog()


def test_package_row_for_a_built_in_product_is_refused(tmp_path: Path) -> None:
    taken = _CATALOG.replace(f"product: {_PRODUCT}", "product: harbor").replace(
        'version: "1.x"', 'version: "2.x"'
    )
    path = _write_package(tmp_path, files={"catalog.yaml": taken})
    _import_package(path)
    with pytest.raises(CatalogError, match=r"row for product/version \('harbor', '2.x'\)"):
        load_catalog()


def test_two_packages_with_the_same_row_are_refused(tmp_path: Path) -> None:
    _import_package(_write_package(tmp_path, "acme_one"))
    _import_package(_write_package(tmp_path, "acme_two"))
    with pytest.raises(
        CatalogError, match=r"already has one from 'meho_backplane\.connectors\.acme_one'"
    ):
        load_catalog()


def test_register_refuses_a_name_outside_the_connectors_folder() -> None:
    with pytest.raises(ValueError, match="is not a connector folder"):
        register_connector_catalog("meho_backplane.operations.ingest")
    with pytest.raises(ValueError, match="is not a connector folder"):
        register_connector_catalog("meho_backplane.connectors.acme.sub")
    assert catalog_module._PACKAGE_CATALOGS == {}


def test_register_refuses_a_file_name_with_a_path() -> None:
    with pytest.raises(ValueError, match="no path separators"):
        register_connector_catalog(_PACKAGE, "sub/catalog.yaml")
    with pytest.raises(ValueError, match="no path separators"):
        register_connector_catalog(_PACKAGE, "../catalog.yaml")
    assert catalog_module._PACKAGE_CATALOGS == {}
