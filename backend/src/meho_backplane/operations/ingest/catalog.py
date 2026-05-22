# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated connector-spec catalog (Goal #214 raw-REST ingest on-ramp; #743).

The catalog maps ``(product, version)`` to the recommended OpenAPI spec
source(s) plus the registered connector class that covers the version
label. It is the operator on-ramp for the generic-ingestion half of the
two-layer connector model: instead of knowing where a vendor hosts its
spec and which connector class covers which version, an operator (or the
``meho connector catalog list`` / ``ingest --catalog`` CLI verbs in #915)
resolves an entry from here.

**Where the data lives.** The data file ships as *package data* next to
this loader (:data:`_CATALOG_RESOURCE`) rather than at the repo-root
``docs/connector-specs/catalog.yaml`` path the Task originally named. The
backend image build context is ``backend/`` and the wheel only packages
``src/meho_backplane``, so a repo-root ``docs/`` file is absent at
runtime; colocating the YAML with its loader and resolving it through
:func:`importlib.resources.files` is the same pattern the Alembic config
uses (:func:`meho_backplane.db.migrations.find_alembic_ini`) and the only
shape that survives into a deployed container. ``docs/cross-repo/
connector-catalog.md`` is the operator-facing pointer.

**Two validation layers.**

* :func:`load_catalog` (called at backplane startup) parses the YAML and
  runs Pydantic schema validation -- unknown fields, a non-PEP-440
  ``spec_info_version``, a duplicate ``(product, version)``, or malformed
  YAML crash startup (and therefore CI's app-boot smoke). It does not
  touch the connector registry, so it is safe to run inside the lifespan
  regardless of import-cache state.
* :func:`validate_catalog_registry_coverage` cross-checks every entry's
  ``requires_connector_class`` against
  :func:`~meho_backplane.connectors.registry.all_connectors_v2`. That is
  the #743 criterion-(b) guard; it runs as a CI regression test (where
  the full connector set is registered) rather than at startup, where the
  registry's populated-ness is import-order-dependent under pytest-xdist.
"""

from __future__ import annotations

import functools
import re
from importlib import resources

import yaml
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Package + resource name of the catalog YAML shipped as package data.
_CATALOG_PACKAGE = "meho_backplane.operations.ingest"
_CATALOG_RESOURCE = "catalog.yaml"

#: A SHA-256 digest is 64 lowercase hex characters.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CatalogError(RuntimeError):
    """Raised when the connector-spec catalog is malformed or incoherent.

    Carries a human-readable remediation message; raised at startup (parse
    failure) and by :func:`validate_catalog_registry_coverage` (registry
    mismatch).
    """


class ConnectorSpecEntry(BaseModel):
    """One curated ``(product, version)`` -> spec-source mapping.

    ``upstream is None`` marks a typed connector with no ingestable spec;
    the CLI's ``ingest --catalog`` path (#915) refuses such an entry rather
    than POSTing an empty ``specs`` list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    impl_id: str = Field(min_length=1, max_length=128)
    requires_connector_class: str = Field(min_length=1, max_length=128)
    upstream: tuple[str, ...] | None = None
    spec_info_version: str | None = Field(default=None, max_length=64)
    sha256: str | None = Field(default=None, max_length=64)
    notes: str = Field(default="", max_length=2048)

    @field_validator("product", "version", "impl_id", "requires_connector_class")
    @classmethod
    def _strip_required_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank or whitespace-only")
        return normalized

    @field_validator("spec_info_version")
    @classmethod
    def _spec_info_version_is_pep440(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"spec_info_version {value!r} is not a valid PEP 440 version") from exc
        return value

    @field_validator("sha256")
    @classmethod
    def _sha256_is_hex_digest(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value

    @field_validator("upstream")
    @classmethod
    def _upstream_entries_nonempty(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("upstream must be null (typed connector) or a non-empty URL list")
        normalized = tuple(url.strip() for url in value)
        if any(not url for url in normalized):
            raise ValueError("upstream URLs must be non-empty")
        return normalized


class ConnectorSpecCatalog(BaseModel):
    """The full catalog: a list of entries with a unique ``(product, version)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ConnectorSpecEntry, ...] = Field(min_length=1)

    @field_validator("entries")
    @classmethod
    def _product_version_unique(
        cls, entries: tuple[ConnectorSpecEntry, ...]
    ) -> tuple[ConnectorSpecEntry, ...]:
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            key = (entry.product, entry.version)
            if key in seen:
                raise ValueError(f"duplicate catalog entry for product/version {key}")
            seen.add(key)
        return entries

    def get(self, product: str, version: str) -> ConnectorSpecEntry | None:
        """Return the entry for ``(product, version)`` or ``None``."""
        for entry in self.entries:
            if entry.product == product and entry.version == version:
                return entry
        return None


def parse_catalog(raw: str) -> ConnectorSpecCatalog:
    """Parse + schema-validate raw catalog YAML.

    Raises :class:`CatalogError` (never a bare ``yaml`` / ``pydantic``
    error) so callers get one remediation-bearing exception type.
    """
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CatalogError(f"connector-spec catalog is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError("connector-spec catalog must be a mapping with an 'entries' key")
    try:
        return ConnectorSpecCatalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"connector-spec catalog failed schema validation: {exc}") from exc


@functools.lru_cache(maxsize=1)
def load_catalog() -> ConnectorSpecCatalog:
    """Load + validate the packaged catalog (cached for the process).

    Called once at backplane startup (a malformed catalog crashes the
    lifespan -> CI app-boot smoke fails) and reused by
    ``GET /api/v1/connectors/catalog``.
    """
    raw = resources.files(_CATALOG_PACKAGE).joinpath(_CATALOG_RESOURCE).read_text(encoding="utf-8")
    return parse_catalog(raw)


def validate_catalog_registry_coverage(catalog: ConnectorSpecCatalog | None = None) -> None:
    """Assert every ``requires_connector_class`` is registered (#743 crit. b).

    Cross-checks against
    :func:`~meho_backplane.connectors.registry.all_connectors_v2`. Raises
    :class:`CatalogError` listing the offenders. Imported lazily so this
    module stays import-light for the startup parse path.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    cat = catalog if catalog is not None else load_catalog()
    registered = {cls.__name__ for cls in all_connectors_v2().values()}
    missing = sorted(
        {
            e.requires_connector_class
            for e in cat.entries
            if e.requires_connector_class not in registered
        }
    )
    if missing:
        raise CatalogError(
            "connector-spec catalog references unregistered connector class(es): "
            f"{missing}; registered classes: {sorted(registered)}"
        )
