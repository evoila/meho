# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Domain exceptions for the G0.7 spec-ingestion pipeline.

Four unrelated families of failure modes share this module so T1
(OpenAPI parser, #401), T2 (registration helper, #403), T4
(review-queue state machine, #402), and the G0.9-T8 spec-vs-label
cross-check agree on an import path:

Parser failures (T1 #401) — raised from
:func:`~meho_backplane.operations.ingest.parse_openapi`:

* :class:`InvalidSpecError` — the document is not a structurally
  valid OpenAPI spec or the local file referenced cannot be read.
* :class:`UnsupportedSpecError` — the document is valid OpenAPI but
  ships a flavour the parser doesn't ingest (Swagger 2.0, OpenAPI
  4.x, cross-document ``$ref``).
* :class:`InvalidSchemaError` — a referenced JSON Schema is
  structurally broken (dangling ``$ref``, component-path drill-down,
  non-list parameters).

Registration failures (T2 #403) — raised from
:func:`~meho_backplane.operations.ingest.register_ingested_operations`:

* :class:`OpIdCollision` — two operations under the same
  ``(product, version, impl_id)`` triple carry the same ``op_id``,
  raised in two distinct branches:

  * **Within-batch** — two ops in a single ingest call collide.
    Caught up-front by a set scan before any DB write.
  * **Cross-call** — a second ingest call under the same triple
    submits an ``op_id`` already persisted from a prior call with
    a different ``spec_source``. Caught per-row after the natural-
    key lookup, before the embedding hash comparison.

  Both branches raise the same exception type so callers can write
  one ``except OpIdCollision`` and handle both. The natural key
  ``(product, version, impl_id, op_id)`` is unique by partial index
  on ``endpoint_descriptor``; merging two specs that happen to
  expose the same ``op_id`` would silently UPDATE the first row
  with the second's payload, which is never what the operator
  wants. The exception names every colliding ``op_id`` so the
  operator can decide whether to rename one (out-of-scope for v0.2)
  or skip the offending spec pair; for cross-call collisions it
  also names both ``spec_source`` values to disambiguate.

Review-queue failures (T4 #402) — raised from
:class:`~meho_backplane.operations.ingest.ReviewService`:

* :class:`InvalidStateTransitionError` — the caller asked for a
  state transition the machine forbids (e.g. ``enabled → staged``).
  This is a programming bug at the call site, not an operator-
  recoverable condition, but the message is kept structured so the
  CLI / API layers (T5 / T6) can map it onto a 400 with a clear
  detail field.
* :class:`ConnectorNotFoundError` — the
  ``(product, version, impl_id)`` triple the caller targeted has no
  matching rows visible to the operator. Returned uniformly for
  three failure modes: (a) the connector genuinely does not exist,
  (b) the connector exists but belongs to a different tenant, or
  (c) the connector is built-in (``tenant_id IS NULL``) and the
  operator lacks the ``tenant_admin`` role required to manage
  built-ins. Conflating cross-tenant access denial with "not
  found" matches the v0.2 tenant-isolation discipline used
  elsewhere in the backplane: an operator must not be able to
  enumerate another tenant's connectors by probing for ``HTTP 404``
  vs ``HTTP 403`` boundaries.

Ingest-pipeline failures (G0.9-T8) — raised from
:meth:`~meho_backplane.operations.ingest.IngestionPipelineService.ingest`:

* :class:`VersionMismatchError` — the operator-supplied ``version``
  label is incompatible with at least one supplied spec's
  ``info.version``, or two specs in the same bundle declare
  incompatible ``info.version`` strings. Mapped onto HTTP 422 at the
  route layer to distinguish it from generic ``400`` parser failures.

The parser, registration, and ingest-pipeline classes inherit from
:class:`ValueError` so callers that already catch parsing errors via
``except ValueError`` keep working; the review-queue classes inherit
from :class:`Exception` directly so callers can ``except`` them
precisely without catching unrelated runtime faults.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

__all__ = [
    "ConnectorNotFoundError",
    "InvalidSchemaError",
    "InvalidSpecError",
    "InvalidStateTransitionError",
    "LlmOutputInvalid",
    "OpIdCollision",
    "UncoveredVersionLabel",
    "UnsupportedSpecError",
    "VersionMismatchError",
]


class InvalidSpecError(ValueError):
    """The document is not a structurally valid OpenAPI spec.

    Raised when the root document lacks the ``paths`` key, isn't a
    mapping, or otherwise fails structural validation that does NOT
    depend on the OpenAPI version (those raise
    :exc:`UnsupportedSpecError`).
    """


class UnsupportedSpecError(ValueError):
    """The document is structurally valid but ships a flavour the parser doesn't ingest.

    Raised for Swagger 2.0, OpenAPI 4.x, cross-document ``$ref``, and
    similar known-unsupported cases. The exception message always
    names the offending shape so the operator can decide whether to
    file a v0.2.next request or pre-process the spec.
    """


class InvalidSchemaError(ValueError):
    """A referenced JSON Schema is structurally broken.

    Raised when a ``$ref`` points at a component that doesn't exist,
    when a path's parameter list isn't a list, or when the spec uses
    a structurally unsupported shape (component-path drill-down
    refs, for example).
    """


class OpIdCollision(ValueError):  # noqa: N818 -- Task #403 API contract pins this name verbatim
    """An ``op_id`` collides with another op under the same connector triple.

    Two raise sites, one exception type:

    * **Within-batch collision** — two operations in a single
      :func:`register_ingested_operations` call share an ``op_id``.
      Caught up-front by ``_detect_op_id_collisions`` (a set scan)
      before any DB write. ``existing_spec_source`` /
      ``incoming_spec_source`` are ``None`` here — both colliding ops
      came from the same ingest call, so the spec-source dimension
      doesn't apply.

    * **Cross-call collision** — a second
      :func:`register_ingested_operations` call under the same
      ``(product, version, impl_id)`` triple submits an ``op_id``
      that's already persisted from a prior call with a different
      ``spec_source``. Caught in ``upsert_one_operation`` (per-row)
      after the natural-key lookup, before the embedding hash
      comparison. Both ``existing_spec_source`` (read off the
      persisted row's ``spec:<src>`` tag) and ``incoming_spec_source``
      (the current call's argument) are set so the operator can see
      which two specs are fighting over the ``op_id``.

    Attributes
    ----------
    op_ids:
        The colliding ``op_id`` values, sorted for stable error
        messages and diff-friendly test assertions. Within-batch
        collisions list every distinct duplicate; cross-call
        collisions list a single ``op_id`` (the row the second
        upsert would clobber).
    product, version, impl_id:
        The connector coordinates being ingested. Surfaced on the
        exception so the operator-facing CLI / API can render a
        complete "couldn't ingest spec X into connector Y because Z"
        message without re-threading the connector identity from
        the call site.
    existing_spec_source:
        For cross-call collisions, the ``spec_source`` of the
        already-persisted row (the prior call's spec). ``None`` for
        within-batch collisions.
    incoming_spec_source:
        For cross-call collisions, the ``spec_source`` of the
        in-flight call (the call that just hit the collision).
        ``None`` for within-batch collisions.

    Inherits from :class:`ValueError` so callers that already catch
    parser-shaped errors via ``except ValueError`` keep working
    without a targeted ``except OpIdCollision``. The targeted class
    still exists so tests can assert on the precise shape and so the
    CLI layer can map it onto a structured 400 detail field.
    """

    def __init__(
        self,
        *,
        op_ids: list[str],
        product: str,
        version: str,
        impl_id: str,
        existing_spec_source: str | None = None,
        incoming_spec_source: str | None = None,
    ) -> None:
        self.op_ids = sorted(op_ids)
        self.product = product
        self.version = version
        self.impl_id = impl_id
        self.existing_spec_source = existing_spec_source
        self.incoming_spec_source = incoming_spec_source
        spec_suffix = ""
        if existing_spec_source is not None or incoming_spec_source is not None:
            spec_suffix = (
                f" between spec_source={existing_spec_source!r} (persisted) "
                f"and spec_source={incoming_spec_source!r} (incoming)"
            )
        super().__init__(
            f"op_id collision while ingesting into "
            f"({product!r}, {version!r}, {impl_id!r}): {self.op_ids!r}"
            f"{spec_suffix}"
        )


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition is forbidden by the machine.

    Attributes
    ----------
    current_status:
        The ``review_status`` the row currently holds.
    requested_status:
        The ``review_status`` the caller asked to move to.
    group_key:
        The ``operation_group.group_key`` whose transition was
        rejected; ``None`` when the rejection covers the whole
        connector rather than a single group (e.g. an
        ``enable_connector`` attempt where at least one child group
        was already in an unsupported state).

    The exception message renders as
    ``"cannot transition '<current>' -> '<requested>' (group=<key>)"``
    so the CLI / API layers can surface a clear operator-facing
    detail without doing their own string assembly.
    """

    def __init__(
        self,
        *,
        current_status: str,
        requested_status: str,
        group_key: str | None = None,
    ) -> None:
        self.current_status = current_status
        self.requested_status = requested_status
        self.group_key = group_key
        suffix = f" (group={group_key})" if group_key is not None else ""
        super().__init__(
            f"cannot transition {current_status!r} -> {requested_status!r}{suffix}",
        )


class LlmOutputInvalid(Exception):  # noqa: N818 -- Task #404 API contract pins this name
    """Raised when an LLM grouping-pass response fails schema validation.

    The chassis LLM client is prompted to emit a tightly-shaped JSON
    payload (an array of group proposals for Pass 1, an op_id-to-
    group_key map for Pass 2). When the model returns prose around the
    JSON, malformed JSON, or a JSON value that fails its Pydantic
    schema, T3 surfaces this exception so the operator-facing CLI / API
    layer can log the raw output and prompt for a retry rather than
    persisting half-baked taxonomy.

    Attributes
    ----------
    pass_name:
        Which of the two passes produced the bad output --
        ``"propose_groups"`` (Pass 1) or ``"assign_ops"`` (Pass 2). Used
        by the CLI's retry prompt and by analytics that bucket prompt
        failures per pass.
    raw_output:
        The verbatim string the LLM returned, before any parsing or
        normalisation. Truncated to 8 KiB in the message to keep the
        exception printable; the full value is preserved on the
        attribute for debug logging.
    parse_error:
        The underlying parser / validator exception, attached as the
        cause for ``raise ... from ...`` chains. Lifted onto a named
        attribute so callers can inspect the structured pydantic
        ``ValidationError`` shape without crawling ``__cause__``.

    Distinct from :class:`InvalidSpecError` and :class:`InvalidSchemaError`
    (which describe upstream OpenAPI breakage) -- this exception is
    specifically about LLM output quality, an operational concern that
    deserves its own retry path.
    """

    _MESSAGE_PREVIEW_LIMIT = 8 * 1024

    def __init__(
        self,
        *,
        pass_name: str,
        raw_output: str,
        parse_error: Exception,
    ) -> None:
        self.pass_name = pass_name
        self.raw_output = raw_output
        self.parse_error = parse_error
        preview = raw_output
        if len(preview) > self._MESSAGE_PREVIEW_LIMIT:
            preview = preview[: self._MESSAGE_PREVIEW_LIMIT] + "...<truncated>"
        super().__init__(
            f"LLM output failed validation in pass {pass_name!r}: "
            f"{parse_error!r}; raw_output={preview!r}",
        )


class UncoveredVersionLabel(ValueError):  # noqa: N818 -- Task #741 names this verbatim
    """The operator's ``version`` label is outside every registered class's range.

    Raised by the ingest pre-flight check (G0.9-T9, #741) before any
    DB write when at least one connector class for the
    ``(product, impl_id)`` pair is already registered against the v2
    registry but none advertises a ``supported_version_range`` that
    accepts the operator-supplied ``version``. Dispatch would later
    raise :exc:`~meho_backplane.connectors.resolver.NoMatchingConnector`
    on the first ``call_operation`` against the orphan rows — the
    pre-flight catches the misconfiguration at the ingest call site
    where the operator can correct it immediately.

    Conditions for raising:

    * **At least one** class with matching ``(product, impl_id)`` is
      registered against the v2 registry.
    * **None** of those classes accepts the operator's ``version``
      label per its PEP 440 ``supported_version_range``.

    The "no class registered for ``(product, impl_id)``" case is the
    v0.4-staging path (ops land before the class exists); the pre-
    flight logs ``connector_ingest_orphaned_class`` and proceeds
    rather than raising — the dispatcher will surface the orphan
    clearly later, and the warning is the upstream signal.

    Attributes
    ----------
    product, version, impl_id:
        The connector triple the operator submitted; surfaced on the
        exception so the CLI / REST layer can render a complete
        operator-facing error without re-threading them from the
        call site.
    candidates:
        Sorted list of ``(version, impl_id, class_name, supported_version_range)``
        tuples — one per existing registered class for the
        ``(product, impl_id)`` pair. Surfaces in the exception
        message so the operator sees exactly which advertised ranges
        the label fell outside of.

    Inherits from :class:`ValueError` so callers that already catch
    ingest-shaped errors via ``except ValueError`` keep working. The
    REST router maps this exception onto HTTP 422 Unprocessable
    Entity — the request body is structurally valid (Pydantic
    accepted it) but semantically rejected at the service layer.
    """

    def __init__(
        self,
        *,
        product: str,
        version: str,
        impl_id: str,
        candidates: list[tuple[str, str, str, str]],
    ) -> None:
        self.product = product
        self.version = version
        self.impl_id = impl_id
        self.candidates = sorted(candidates)
        rendered_candidates = ", ".join(
            f"{cls_name} (version={cand_version!r}, impl_id={cand_impl_id!r}, "
            f"supported_version_range={spec!r})"
            for cand_version, cand_impl_id, cls_name, spec in self.candidates
        )
        super().__init__(
            f"version={version!r} is not covered by any registered "
            f"connector class for product={product!r}, impl_id={impl_id!r}; "
            f"registered: [{rendered_candidates}]. Either register a class "
            f"with a compatible supported_version_range or pick a version "
            f"inside an existing class's range."
        )


class ConnectorNotFoundError(Exception):
    """Raised when the requested connector has no rows visible to the operator.

    Attributes
    ----------
    connector_id:
        The operator-facing connector identifier (e.g.
        ``"vmware-rest-9.0"``).
    tenant_id:
        The tenant scope the caller asked for. ``None`` indicates a
        built-in scope; a UUID indicates a tenant-curated scope.

    See the module docstring for the three failure modes this
    exception covers; the caller cannot tell them apart from the
    exception alone, which is intentional.
    """

    def __init__(
        self,
        *,
        connector_id: str,
        tenant_id: UUID | None,
    ) -> None:
        self.connector_id = connector_id
        self.tenant_id = tenant_id
        scope = "built-in" if tenant_id is None else f"tenant={tenant_id}"
        super().__init__(f"connector {connector_id!r} not found ({scope})")


class VersionMismatchError(ValueError):
    """Raised when ingest specs disagree with the operator-supplied version label.

    Two raise sites under one exception type so the route layer maps
    both onto a single ``HTTP 422 Unprocessable Entity`` response with
    a structured detail an operator can act on:

    * **Spec/label mismatch** — the operator labelled the ingest
      ``version=X`` but at least one supplied spec declares
      ``info.version=Y`` whose major component disagrees with ``X``.
      Per the G0.9-T8 contract the resolver's tie-break math
      (``packaging.version.Version`` parsed as ``>=N.0,<N+1.0``) is
      the source of truth for "compatible"; cross-major drift always
      fails. Inexact-but-compatible drift (same major, different
      minor) is logged via the ``connector_ingest_version_drift``
      structured event and ingests through — *not* raised — so this
      exception only covers the hard-fail case.

    * **Multi-spec inconsistency** — two or more specs in the same
      ingest declare ``info.version`` strings that don't share a
      major version. The bundle is internally inconsistent; the
      operator either supplied the wrong specs together or labelled
      them wrong, and no single connector triple can house them
      faithfully.

    The exception's ``kind`` attribute distinguishes the two raise
    sites for tests / structured logs / operator-facing messaging.

    Attributes
    ----------
    kind:
        ``"spec_label_mismatch"`` or ``"multi_spec_inconsistent"``.
    requested_version:
        The operator-supplied ``IngestRequest.version`` label.
    spec_info_versions:
        Sorted list of ``(spec_uri, info_version)`` pairs for the
        specs participating in the failure. For ``spec_label_mismatch``
        only the mismatching specs are listed; for
        ``multi_spec_inconsistent`` every spec in the bundle is
        listed so the operator can see the conflict at a glance.

    Inherits from :class:`ValueError` so callers that already catch
    parser-shaped errors via ``except ValueError`` keep working. The
    targeted class still exists so the route layer can map it onto
    ``422`` specifically (validation-shaped client error) rather than
    the ``400`` other ValueError children land on.
    """

    def __init__(
        self,
        *,
        kind: str,
        requested_version: str,
        spec_info_versions: Sequence[tuple[str, str | None]],
        suggestion: str | None = None,
    ) -> None:
        self.kind = kind
        self.requested_version = requested_version
        self.spec_info_versions = sorted(spec_info_versions, key=lambda pair: pair[0])
        self.suggestion = suggestion
        rendered_specs = ", ".join(
            f"{uri!r} -> info.version={version!r}" for uri, version in self.spec_info_versions
        )
        if kind == "spec_label_mismatch":
            base = (
                f"spec/label version mismatch: requested_version={requested_version!r} "
                f"is incompatible with [{rendered_specs}]"
            )
        elif kind == "multi_spec_inconsistent":
            base = (
                f"multi-spec ingest is internally inconsistent under "
                f"requested_version={requested_version!r}: [{rendered_specs}]"
            )
        else:  # pragma: no cover -- defensive; constructor is called with a fixed kind set
            base = (
                f"version mismatch ({kind}): requested_version={requested_version!r}, "
                f"specs=[{rendered_specs}]"
            )
        if suggestion is not None:
            base = f"{base}; {suggestion}"
        super().__init__(base)
