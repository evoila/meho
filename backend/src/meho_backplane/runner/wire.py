# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Versioned wire models for the satellite-runner poll/report protocol.

One codebase, one schema. These models are the single source of truth
for both sides of the runner wire:

* the **runner** (this package) serialises results and deserialises
  assignments against them, and
* the central **ingest + assignment API** (#2499) MUST import these same
  classes for its serialiser rather than declaring a parallel twin.

#2499 is expected to **widen** these models in place — most notably
:class:`ResolvedTargetDescriptor`, which is seeded here with the
resolver-read attributes a handler duck-reads and grows the
connection-routing set (host/port/secret_ref/TLS) on the central side.
Widening one class both sides import keeps the two ends of the wire in
lockstep by construction; forking a second copy would let the shapes
drift silently. Do not fork.

``SCHEMA_VERSION`` is the wire version: the version of these models.
The runner sends it on every call in the :data:`WIRE_VERSION_HEADER`
header. The central MEHO compares it with its own ``SCHEMA_VERSION``
(:func:`~meho_backplane.auth.runner_guard.runner_wire_version`, before it
reads the request body) and, when they differ, refuses the call with HTTP
409. The refusal text starts with :data:`WIRE_VERSION_MISMATCH` and names
both versions (see :func:`wire_version_mismatch_message`). The runner logs
that text, drops its cached work and runs nothing for that tick, whichever
call was refused. A call without the header counts as version
:data:`WIRE_VERSION_WHEN_MISSING`, so runners built before the header
existed keep working.

The check guards only the version number. The models still drop unknown
fields without a message (the pydantic default), so a change a runner must
not silently ignore -- a new field it has to act on, a changed meaning --
needs a ``SCHEMA_VERSION`` bump.

Next to its wire version the runner sends the digest of its connector
packages (:mod:`meho_backplane.runner.package_set`, #5038). A work item for
an operation that a connector package provides names the package set it
needs in :attr:`RunnerWorkItem.package_set`. This field did not need a
``SCHEMA_VERSION`` bump: a runner that sends no digest counts as a runner
without connector packages, so the central MEHO never assigns it such an
item, and an older runner never sees the field set.

The per-assignment :attr:`RunnerAssignment.assignment_version` is a
separate concern: an **opaque content digest** whose contract (a sha256
over the canonical materialised payload) is owned by #2499. The runner
treats it purely as a cache key — it never parses it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    Strict,
    ValidationError,
    model_validator,
)

from meho_backplane.auth.operator import PrincipalKind, TenantRole

__all__ = [
    "FEATURES_HEADER",
    "FEATURE_SCHEDULED_CHECKS",
    "SCHEMA_VERSION",
    "SET_ASIDE_NOTICES_MAX",
    "WIRE_VERSION_HEADER",
    "WIRE_VERSION_MISMATCH",
    "WIRE_VERSION_WHEN_MISSING",
    "ResolvedTargetDescriptor",
    "RunnerAssignment",
    "RunnerPrincipal",
    "RunnerResult",
    "RunnerResultBatch",
    "RunnerWorkItem",
    "wire_version_mismatch_message",
]

#: Wire protocol version. Bump on any incompatible change to the models
#: below. The runner sends it on every call and the central MEHO refuses a
#: call with another version (HTTP 409 naming both versions), so a runner
#: and a central MEHO on different versions stop with a clear message
#: instead of misreading fields. Keep it a whole number: the two sides
#: compare it as one to say which side to update.
SCHEMA_VERSION = "1"

#: Request header that carries the runner's wire version.
WIRE_VERSION_HEADER = "X-MEHO-Runner-Wire-Version"

#: The version a call without :data:`WIRE_VERSION_HEADER` counts as. Every
#: runner built before the header existed spoke version ``"1"``.
WIRE_VERSION_WHEN_MISSING = "1"

#: Request header in which the runner names what it can do, as a list of
#: words with a comma between them. A runner without the header can do none
#: of them. A new word needs no ``SCHEMA_VERSION`` bump: the central MEHO
#: sends work that needs a word only to a runner that names it.
FEATURES_HEADER = "X-MEHO-Runner-Features"

#: The runner runs a work item only when it is due (see
#: :attr:`RunnerWorkItem.next_due_at`), cleans secret values from the answer
#: before it keeps or sends it, and sends a list answer as it is. The
#: central MEHO sends the work of the checks it counts only to a runner that
#: names this word, so an older runner never runs a daily check on every
#: poll.
FEATURE_SCHEDULED_CHECKS = "scheduled-checks"

#: First word of the central MEHO's refusal text on a version mismatch.
#: The runner client looks for it to tell this refusal apart from other
#: HTTP 409 answers.
WIRE_VERSION_MISMATCH = "runner_wire_version_mismatch"


def wire_version_mismatch_message(runner_version: str, central_version: str) -> str:
    """Return the refusal text for a runner on another wire version.

    Names both versions and says which side to update, for example
    ``runner_wire_version_mismatch: runner wire version 1, central MEHO
    needs 2: update the runner``.
    """
    if runner_version.isdigit() and central_version.isdigit():
        older = int(runner_version) < int(central_version)
        advice = "update the runner" if older else "update the central MEHO"
    else:
        advice = "run the same MEHO version on the runner and the central MEHO"
    return (
        f"{WIRE_VERSION_MISMATCH}: runner wire version {runner_version}, "
        f"central MEHO needs {central_version}: {advice}"
    )


class ResolvedTargetDescriptor(BaseModel):
    """Centrally-resolved target attributes a connector handler reads.

    The runner has no local target table, so the central resolver
    (:mod:`meho_backplane.connectors.resolver`, DB-bound) materialises
    the fields a handler duck-reads off a ``Target`` row and ships them
    on the assignment. It carries the resolver-read attributes
    (``product`` / ``fingerprint`` / ``version`` / ``preferred_impl_id``)
    the tie-break ladder consumes **and** the connection-routing set
    (``host`` / ``port`` / ``fqdn`` / ``secret_ref`` / ``auth_model`` /
    TLS flags) the HTTP adapter reads, so on the runner the descriptor
    feeds :func:`~meho_backplane.connectors.resolver.resolve_connector`
    and the connector adapters directly — no DB session, no second
    resolution mechanism.

    The full field set is moulded on the central
    :class:`~meho_backplane.targets.schemas.TargetSummary` (#2499): the
    same identification + connection-routing projection of the
    ``Target`` row, minus the server-managed timestamps and the
    operator-authored ``notes`` (neither drives connector routing).

    Credential discipline: ``secret_ref`` is the **reference** the runner
    resolves outbound under its own read-only scope; no credential value
    is ever embedded (the runner spools assignments on disk, so a value
    would durably persist — a locked no-durable-artifact violation).
    """

    model_config = ConfigDict(frozen=True)

    # Identity (stable cross-system keys the runner echoes in reports /
    # audit; ``id`` + ``tenant_id`` mirror the ``Target`` row PK + scope).
    id: UUID
    tenant_id: UUID
    name: str
    aliases: tuple[str, ...] = ()
    # Resolver inputs: the tie-break ladder reads ``product`` +
    # ``fingerprint.version`` / ``version`` + ``preferred_impl_id``.
    product: str
    version: str | None = None
    fingerprint: dict[str, Any] | None = None
    preferred_impl_id: str | None = None
    # Connection routing: the HTTP adapter builds ``https://{host}[:{port}]``
    # and reads the TLS trust knobs off these fields.
    host: str
    port: int | None = None
    fqdn: str | None = None
    secret_ref: str | None = None
    auth_model: str = "shared_service_account"
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    # #3711. Secret field names map the credential reader applies to the
    # target's own secret. Field names only, never a value.
    secret_field_map: dict[str, str] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)
    # When the target row last changed (#3879). The runner compares it with
    # the value it saw last and drops the login it keeps for the target when
    # the row changed. ``None`` from an older gateway: never treated as a
    # change.
    updated_at: datetime | None = None
    # When a person last changed the target; a probe does not move it. The
    # runner drops its kept login when this changes. ``None`` from an older
    # gateway: ``updated_at`` is used instead.
    connection_changed_at: datetime | None = None


class RunnerPrincipal(BaseModel):
    """Principal context an executed op runs under, carried on each item.

    Enough to reconstruct an :class:`~meho_backplane.auth.operator.Operator`
    on the runner without a JWT: the runner never sees a bearer token for
    the acting principal (the op was already authorized centrally), so the
    reconstructed operator's ``raw_jwt`` is empty.
    """

    model_config = ConfigDict(frozen=True)

    sub: str
    tenant_id: UUID
    tenant_role: TenantRole
    principal_kind: PrincipalKind = PrincipalKind.SERVICE


class RunnerWorkItem(BaseModel):
    """One centrally-authorized operation for the runner to execute.

    Carries the fields the runner needs to resolve and invoke a handler
    entirely locally: the dotted ``handler_ref`` (resolved via
    :func:`~meho_backplane.operations._handler_resolve.import_handler`),
    the ``(product, version, impl_id)`` registry key used to rebind a
    bound-method handler against its connector instance, the validated
    ``params``, the ``safety_level`` the runner re-checks (defence in
    depth), the principal context, and the resolved target descriptor
    (``None`` for targetless synthetic ops such as ``net.*``).

    Remote-write signing (#3189): a ``caution`` / ``remote-write`` item
    additionally carries the centre's Ed25519 ``signature`` over its
    canonical payload and the ``expires_at`` freshness bound the signature
    covers, both verified in
    :func:`~meho_backplane.runner.executor._screen_item` before execution.
    Both are ``None`` for a ``safe`` read item (the recurring assignment
    path materialises only ``safe`` ops and never signs them) -- signing is
    a write-tier property, so their absence on a ``safe`` item is correct,
    not a missing field.

    Connector packages (#5038): ``package_set`` is the digest of the
    connector package set the item needs
    (:func:`~meho_backplane.runner.package_set.package_set_digest`). The
    central MEHO sets it only when a connector package provides the op, and
    the runner refuses the item when its own digest differs. ``None`` for an
    op built into MEHO.
    """

    model_config = ConfigDict(frozen=True)

    check_ref: str
    op_id: str
    product: str
    version: str = ""
    impl_id: str = ""
    handler_ref: str
    params: dict[str, Any] = Field(default_factory=dict)
    safety_level: str
    principal: RunnerPrincipal
    target_descriptor: ResolvedTargetDescriptor | None = None
    signature: str | None = None
    expires_at: datetime | None = None
    package_set: str | None = None
    #: The schedule of an item for a check that MEHO counts: the check's own
    #: ``interval_seconds``, or ``cron_expr`` with ``timezone``. The runner
    #: runs such an item only when it is due, also from a cached assignment.
    #: An item with neither runs on every tick.
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    #: The next due time of a check that MEHO counts, on MEHO's clock. The
    #: runner runs the item once this time has come, also on its first poll
    #: and after a restart. ``meho sensor run`` and the shorter retry of a
    #: waiting state change move it forward. Set only for such a check, and
    #: only for a runner that names :data:`FEATURE_SCHEDULED_CHECKS`.
    next_due_at: datetime | None = None


class RunnerAssignment(BaseModel):
    """The runner's current work assignment, fetched each tick.

    ``assignment_version`` is an opaque digest the runner echoes back as
    ``known_version`` so central can answer an unchanged assignment with a
    ``304`` (see :meth:`RunnerClient.fetch_assignment`).
    """

    model_config = ConfigDict(frozen=True)

    assignment_version: str
    items: list[RunnerWorkItem] = Field(default_factory=list)


class RunnerResult(BaseModel):
    """The outcome of executing one :class:`RunnerWorkItem`.

    ``result_uid`` is generated on the runner (a uuid4 hex) so central
    ingest can deduplicate spool re-posts idempotently — a batch written
    to the retry spool carries the same uids when it is re-posted.

    ``status`` is a runner-level tri-state, distinct from any status
    inside ``result``:

    * ``ok`` — the handler ran and returned its structured payload
      (which may itself report a failed probe; a failed check is a
      result, not a runner error).
    * ``refused`` — the runner declined to execute (unsafe safety_level,
      or a handler_ref outside the connector tree).
    * ``error`` — the handler raised; the exception is summarised in
      ``error`` and never re-raised into the tick loop.
    """

    model_config = ConfigDict(frozen=True)

    result_uid: str
    check_ref: str
    op_id: str
    status: str
    #: The answer of the operation: an object, or a list for an operation
    #: that answers with a list.
    result: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    #: When the runner made the result, on the runner's clock. Central never
    #: uses it as the time of the check (it uses the time it received the
    #: result). It only refuses a time in the future and drops a result made
    #: before the last one it counted. MEHO stores a result without it, but
    #: does not count it.
    produced_at: datetime | None = None
    #: A code for the kind of error, set only for a refused login
    #: (``connector_auth_failed``). MEHO pauses a check after a few of them.
    error_code: str | None = None


class RefusedResult(BaseModel):
    """A result MEHO refused, named in the answer to a result batch.

    ``reason`` is ``future_time``: the runner time of the result is later
    than MEHO's clock. MEHO neither stores nor counts such a result.
    """

    model_config = ConfigDict(frozen=True)

    result_uid: str
    reason: str


#: The most set-aside notices of one post: 1,000. The runner sends no more in
#: one post, and MEHO reads only the first 1,000.
SET_ASIDE_NOTICES_MAX = 1000


def _count_or_none(value: object) -> object:
    """*value* when it is a whole number, else ``None``. ``true`` and ``1.5`` are no count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _time_or_none(value: object) -> datetime | None:
    """*value* when it is a date-time text or a ``datetime``, else ``None``.

    A number, or a text of digits, is no time: it is never read as seconds
    since 1970.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class SetAsideNotice(BaseModel):
    """A result of a batch that MEHO refused every time, so the runner set it aside.

    The runner keeps the batch in its set-aside folder and sends only this
    notice: which check, which result, the status and the code MEHO answered
    with (never the free text of the answer), and the runner time of the result.
    """

    model_config = ConfigDict(frozen=True)

    check_ref: str
    result_uid: str
    #: Only a JSON number: a text such as ``"400"`` makes the notice invalid.
    status: Annotated[int, Strict()]
    code: str | None = None
    produced_at: datetime | None = None


def _valid_notices(entries: list[Any]) -> tuple[list[SetAsideNotice], int]:
    """The entries of *entries* that are valid notices, and how many are not.

    An entry that is not an object, misses a field or has a field of another
    type is not a valid notice.
    """
    valid: list[SetAsideNotice] = []
    for entry in entries:
        try:
            valid.append(SetAsideNotice.model_validate(entry))
        except ValidationError:
            continue
    return valid, len(entries) - len(valid)


class RunnerResultBatch(BaseModel):
    """A batch of results the runner reports (or spools) in one POST.

    ``set_aside`` names the results of batches the runner set aside since its
    last notice. ``set_aside_total`` and ``set_aside_last_at`` say how many
    batches are in the runner's set-aside folder and when the newest one was
    set aside. All three stay empty on a batch without a notice, with one
    exception: after a start, the runner's first notice post sets
    ``set_aside_total`` and ``set_aside_last_at`` also when it carries no
    notice, so MEHO has the count of the folder again. MEHO reads at
    most the first 1,000 entries of ``set_aside`` and skips the rest before it
    checks any entry. Then it checks the entries one by one and skips each
    entry that is not a valid notice; the other entries are kept. It keeps a
    ``set_aside_total`` that is a whole number from 0 to 2147483647 and a
    ``set_aside_last_at`` that is a date-time text with a time zone; it drops
    any other value of these two (also ``true`` or a number) and still takes
    the batch.
    """

    model_config = ConfigDict(frozen=True)

    runner_id: str
    results: list[RunnerResult] = Field(default_factory=list)
    set_aside: list[SetAsideNotice] = Field(default_factory=list)
    set_aside_total: Annotated[int | None, BeforeValidator(_count_or_none)] = None
    set_aside_last_at: Annotated[datetime | None, BeforeValidator(_time_or_none)] = None
    _set_aside_skipped: int = PrivateAttr(default=0)

    @model_validator(mode="wrap")
    @classmethod
    def _read_first_notices(
        cls, data: Any, read: ModelWrapValidatorHandler[RunnerResultBatch]
    ) -> RunnerResultBatch:
        """Keep the first 1,000 entries of ``set_aside``, then the valid notices among them."""
        notices = data.get("set_aside") if isinstance(data, dict) else None
        skipped = 0
        if isinstance(notices, list):
            skipped = max(0, len(notices) - SET_ASIDE_NOTICES_MAX)
            valid, invalid = _valid_notices(notices[:SET_ASIDE_NOTICES_MAX])
            skipped += invalid
            data = {**data, "set_aside": valid}
        batch = read(data)
        batch._set_aside_skipped = skipped
        return batch

    @property
    def set_aside_skipped(self) -> int:
        """How many entries of ``set_aside`` were skipped: after the first 1,000, or not valid."""
        return self._set_aside_skipped
