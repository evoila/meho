# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Test catalog for the shipped-spec and shipped-profile path.

The ``_fixture/1.0`` row checks that a catalog row can ship its own OpenAPI
file (``spec_resource``) and its own execution profile
(``profile_resource``). It is test data, so it lives here and not in MEHO's
own ``catalog.yaml``.

The rows are parsed with this folder as their package. So
:func:`~meho_backplane.operations.ingest.catalog.load_entry_spec` and
:func:`~meho_backplane.operations.ingest.catalog.load_entry_profile` read
the files from this folder, the same way they read the files of a row that
a connector package brings.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import yaml

from meho_backplane.operations.ingest.catalog import (
    ConnectorSpecCatalog,
    ConnectorSpecEntry,
    parse_catalog,
)

#: The dotted name of this folder. Rows parsed with it read their files here.
FIXTURE_CATALOG_PACKAGE = __name__

#: The OpenAPI file of the ``_fixture/1.0`` row. It has one operation: ``GET /things``.
FIXTURE_SPEC_RESOURCE = "fixture_spec.yaml"

#: The execution profile of the ``_fixture/1.0`` row (``basic`` auth).
FIXTURE_PROFILE_RESOURCE = "fixture_profile.yaml"


def load_fixture_catalog() -> ConnectorSpecCatalog:
    """Return the test catalog in this folder: the one ``_fixture/1.0`` row."""
    raw = files(__name__).joinpath("catalog.yaml").read_text(encoding="utf-8")
    return parse_catalog(raw, package=__name__)


def fixture_entry() -> ConnectorSpecEntry:
    """Return the ``_fixture/1.0`` row of the test catalog."""
    entry = load_fixture_catalog().get("_fixture", "1.0")
    assert entry is not None
    return entry


def catalog_with_fixture_files(*rows: dict[str, Any]) -> ConnectorSpecCatalog:
    """Build a catalog from *rows* whose resource names point at files in this folder.

    For tests that need a row with another product name but the same
    shipped spec or profile.
    """
    return parse_catalog(yaml.safe_dump({"entries": list(rows)}), package=__name__)


def read_fixture_profile() -> str:
    """Return the text of the ``_fixture/1.0`` execution profile."""
    return files(__name__).joinpath(FIXTURE_PROFILE_RESOURCE).read_text(encoding="utf-8")
