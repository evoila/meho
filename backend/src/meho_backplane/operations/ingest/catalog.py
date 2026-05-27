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
* :func:`validate_catalog_registry_coverage` cross-checks every entry
  against :func:`~meho_backplane.connectors.registry.all_connectors_v2`
  on two axes:

  - **Class presence** (#743 criterion (b)) — the
    ``requires_connector_class`` resolves to a registered connector class.
  - **Triple registration** (G3.11-T10 #1253) — the ``(product, version,
    impl_id)`` triple itself has an entry in the v2 registry table.
    T8 #1242 surfaced this gap: T1 #1221 registered ``("gh", "3",
    "gh-rest")`` while T3 #1223 wrote the catalog row as ``version: v3``;
    the class-presence check passed because ``GitHubRestConnector`` was
    registered (under a different version key), and the drift only
    surfaced at first dispatch. The triple check catches the class of
    bug at boot / CI time.

  Failure raises :class:`CatalogError` with a ``catalog_registry_triple
  _mismatch:`` (or ``unregistered connector class``) code prefix per
  the G0.14-T11 #1141 error-message-shape convention. At chassis
  startup the lifespan calls this after :func:`_eager_import_connectors`
  has populated the registry, so any mismatch fails the boot rather
  than the first ``POST /api/v1/operations/call``. The same call from
  the test suite exercises the synthetic-drift unit test and the
  shipped-catalog self-test.
"""

from __future__ import annotations

import difflib
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


class CatalogListResponse(BaseModel):
    """Wire envelope for ``GET /api/v1/connectors/catalog``.

    Wrapped in ``catalog`` (not a bare list) so future paging fields can
    land non-breakingly, mirroring the ``GET /`` list shape. Used as the
    route's ``response_model`` so the OpenAPI contract explicitly types
    the envelope + entry fields (rather than a free-form object map).
    """

    model_config = ConfigDict(frozen=True)

    catalog: tuple[ConnectorSpecEntry, ...]


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


def _format_triple(product: str, version: str, impl_id: str) -> str:
    """Render a ``(product, version, impl_id)`` triple for error messages.

    A standalone helper so the unit test, the failure message, and the
    closest-match hint all render the same shape — drift between the
    three is itself a class of bug we're guarding against here.
    """
    return f"(product={product!r}, version={version!r}, impl_id={impl_id!r})"


def _closest_registered_triple(
    target_product: str,
    target_version: str,
    target_impl_id: str,
    registered_triples: list[tuple[str, str, str]],
) -> tuple[str, str, str] | None:
    """Return the closest registered triple to ``target`` (or ``None``).

    Used to populate the closest-match hint in the
    ``catalog_registry_triple_mismatch`` envelope so the operator can
    see *which field drifted*. The matching strategy is two-pass:

    1. **Prefer same product.** When the registry has at least one
       triple sharing the catalog row's ``product`` slug, return the
       closest among them (matched on the concatenated
       ``version|impl_id`` string). This is the T8 #1242 motivating
       case: catalog says ``("gh", "v3", "gh-rest")`` and registry has
       ``("gh", "3", "gh-rest")`` — same product, the version drifted.

    2. **Fall back to global closest.** No same-product match (e.g.
       the operator typo'd the product slug itself) → match against
       every registered triple's joined ``product|version|impl_id``
       string. Returns ``None`` only when the registry is empty.

    Why a hint rather than a fuzzy-resolution: detection only. The
    operator fixes the source of truth (catalog YAML or the
    ``register_connector_v2`` call); the validator never auto-patches.
    """
    if not registered_triples:
        return None

    same_product = [t for t in registered_triples if t[0] == target_product]
    if same_product:
        # Match on version|impl_id when the product agrees — this is the
        # T8 #1242 shape (version-string drift on the same product).
        target_tail = f"{target_version}|{target_impl_id}"
        candidates = {f"{v}|{i}": (p, v, i) for (p, v, i) in same_product}
        match = difflib.get_close_matches(target_tail, list(candidates), n=1, cutoff=0.0)
        if match:
            return candidates[match[0]]
        # `get_close_matches` returned nothing (very rare with cutoff=0.0;
        # only when `candidates` is empty, which `same_product` truthiness
        # already excluded). Fall through to global match.

    # No same-product entry, or no candidate beat the cutoff — match on
    # the full triple string against every registered entry.
    target_joined = f"{target_product}|{target_version}|{target_impl_id}"
    joined_to_triple = {f"{p}|{v}|{i}": (p, v, i) for (p, v, i) in registered_triples}
    match = difflib.get_close_matches(target_joined, list(joined_to_triple), n=1, cutoff=0.0)
    if match:
        return joined_to_triple[match[0]]
    return None


def validate_catalog_registry_coverage(catalog: ConnectorSpecCatalog | None = None) -> None:
    """Assert every catalog entry resolves cleanly against the v2 registry.

    Two axes are checked per the G3.11-T10 #1253 extension:

    * **Class presence** (#743 criterion (b)) —
      ``requires_connector_class`` resolves to a class in
      :func:`~meho_backplane.connectors.registry.all_connectors_v2`.
    * **Triple registration** (T10 #1253) — the row's ``(product,
      version, impl_id)`` triple is itself a key in
      :func:`all_connectors_v2`. This catches the T8 #1242 class of
      bug: a catalog/registry version-string drift that the
      class-only check missed (the class was registered, just under a
      different version key).

    Failure raises :class:`CatalogError`. For the triple-mismatch
    branch, the message carries a ``catalog_registry_triple_mismatch:``
    code prefix and names the catalog triple plus the closest
    registered triple for the same product so the operator can see
    which field drifted, per the
    ``docs/codebase/error-message-shape.md`` convention.

    Imported lazily so this module stays import-light for the startup
    parse path.
    """
    from meho_backplane.connectors.registry import all_connectors_v2

    cat = catalog if catalog is not None else load_catalog()
    registry_v2 = all_connectors_v2()
    registered_class_names = {cls.__name__ for cls in registry_v2.values()}

    # Axis 1: class presence (existing #743 criterion (b) check).
    missing_classes = sorted(
        {
            e.requires_connector_class
            for e in cat.entries
            if e.requires_connector_class not in registered_class_names
        }
    )
    if missing_classes:
        raise CatalogError(
            "connector-spec catalog references unregistered connector class(es): "
            f"{missing_classes}; registered classes: {sorted(registered_class_names)}"
        )

    # Axis 2: triple registration (T10 #1253). Walk catalog rows; flag
    # any whose (product, version, impl_id) triple isn't in the v2
    # registry table. The two checks are layered (class first, then
    # triple) so a totally-missing class surfaces its specific code
    # rather than a triple miss that points at the wrong thing.
    registered_triples = sorted(registry_v2.keys())
    triple_mismatches: list[tuple[ConnectorSpecEntry, tuple[str, str, str] | None]] = []
    for entry in cat.entries:
        triple = (entry.product, entry.version, entry.impl_id)
        if triple not in registry_v2:
            hint = _closest_registered_triple(
                entry.product, entry.version, entry.impl_id, registered_triples
            )
            triple_mismatches.append((entry, hint))

    if triple_mismatches:
        # Build the message in three clauses per the T11 convention:
        # (a) the offending values, (b) the closest-registered hint,
        # (c) the doc reference + remediation imperative.
        catalog_path = "backend/src/meho_backplane/operations/ingest/catalog.yaml"
        register_path = "connectors/<product>/__init__.py"
        details: list[str] = []
        for entry, hint in triple_mismatches:
            catalog_triple = _format_triple(entry.product, entry.version, entry.impl_id)
            if hint is not None:
                hint_str = _format_triple(*hint)
                details.append(
                    f"catalog row {catalog_triple}; closest registered triple: {hint_str}"
                )
            else:
                # Registry empty (rare; mostly test scaffolding) or no
                # close match found — still surface the catalog triple
                # and the full registered list so the operator has the
                # raw data to debug from.
                details.append(
                    f"catalog row {catalog_triple}; no close registered triple "
                    f"(registered triples: {registered_triples})"
                )
        joined = "; ".join(details)
        raise CatalogError(
            "catalog_registry_triple_mismatch: connector-spec catalog row(s) name a "
            f"(product, version, impl_id) triple not present in the v2 connector registry: "
            f"{joined}. Reconcile {catalog_path} with the matching "
            f"register_connector_v2(...) call in {register_path}. See "
            f"docs/codebase/error-message-shape.md for the envelope convention."
        )
