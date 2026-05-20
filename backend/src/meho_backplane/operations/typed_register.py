# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: load-bearing registration surface. The three
# helpers (register_typed_operation, register_composite_operation,
# _register_in_session) are sequential phase pipelines — validate,
# derive handler_ref, fail-closed checks, resolve group, body-hash
# skip, embed, upsert. They share a 17-19-arg natural key + per-op
# metadata surface that the dispatcher and search_operations meta-tool
# read. Splitting them by phase forces every caller to thread the
# same args through a coordinator + four sub-helpers, which makes the
# upsert state machine harder to read, not easier. The two public
# helpers + the private upsert path stay colocated for that reason.

"""``register_typed_operation()`` / ``register_composite_operation()`` -- async upsert helpers.

G0.6-T4 (#395) of Initiative #388 shipped :func:`register_typed_operation`,
the helper typed connectors (VaultConnector, KubernetesConnector,
future bind9 / pfSense / Holodeck) call at init time -- once per
operation it exposes -- to upsert the row into ``endpoint_descriptor``
that the dispatcher (T5, #396) and the ``search_operations`` meta-tool
(T8, #399) read. G3.1-T4 (#504) added the sibling
:func:`register_composite_operation`, which writes the same table with
``source_kind="composite"`` for hand-authored composites that orchestrate
sub-ops via the :class:`~meho_backplane.operations.composite.DispatchChild`
contract. Both helpers share one private upsert path
(:func:`_register_in_session`) so the body-hash skip, group resolution,
and embedding logic stay in lock-step.

The registrar mechanism (:func:`register_typed_op_registrar` /
:func:`run_typed_op_registrars`) carries its v0.2 name for historical
reasons -- it's generic over any async registrar callable and accepts
composite registrants without modification. The rename to a
kind-neutral identifier is a documentation-only cleanup deferred to
v0.2.next; composite connector packages register against the existing
names today.

Algorithm
---------

For each ``(product, version, impl_id, op_id)`` natural key (with
``tenant_id IS NULL`` -- typed registrations are always built-in /
global):

1. Compose the embedding text from
   ``summary + description + custom_description + tags`` via
   :func:`~meho_backplane.operations.embed.build_embedding_text`.
2. SHA-256 the composed text into the incoming ``body_hash``.
3. Derive ``handler_ref`` from the supplied callable's
   ``__module__`` + ``__qualname__``. Reject closures /
   ``functools.partial`` / lambdas / nested defs -- the dispatcher
   resolves ``handler_ref`` at dispatch time via
   ``importlib.import_module + getattr``, which can only round-trip
   module-level functions and bound methods of a class registered
   via the connector registry.
4. Resolve ``group_key`` -> ``operation_group.id`` (lookup + create
   if absent, with ``review_status='enabled'`` for built-in groups).
5. Look up the existing ``endpoint_descriptor`` row by the natural
   key:

   * **No existing row** -- compute the embedding, INSERT the row
     with every field populated.
   * **Existing row, body unchanged** (recomputed hash of the
     persisted ``summary`` / ``description`` / ``custom_description``
     / ``tags`` matches the incoming hash) -- **skip the embedding
     compute**, UPDATE non-embedding fields (parameter_schema,
     response_schema, safety_level, requires_approval,
     llm_instructions, handler_ref, group_id, tags-when-unchanged)
     when changed, advance ``updated_at``. The "skip re-embed"
     branch is the operationally critical path for connector init
     speed on restart -- a connector with 50 typed ops avoids 50
     ONNX inferences (~500-2500 ms total CPU) every pod restart
     when descriptions are unchanged.
   * **Existing row, body changed** -- compute the embedding,
     UPDATE every body-derived field plus ``embedding``.

6. Commit (helper-owned session) or flush (caller-owned session).

Idempotency invariant
---------------------

``register_typed_operation(...)`` called twice in a row with
identical args is a no-op for the embedding pipeline: one row in
the table, ``EmbeddingService.encode_one`` called exactly once
across both calls. The second call still UPDATEs ``updated_at`` so
operators can grep "when did this op last get reregistered"; the
expensive work (embedding) is skipped.

Handler dotted-path contract
----------------------------

``handler_ref`` is ``f"{handler.__module__}.{handler.__qualname__}"``
for module-level functions and ``f"{handler.__module__}.{handler.
__qualname__}"`` for bound methods (the bound-method form's
``__qualname__`` already includes the class -- ``"VaultConnector.
kv_read"`` -- which is what ``getattr`` chained resolution needs).

The dispatcher's resolution path (in T5) splits on ``.``, imports
the module via :func:`importlib.import_module`, then walks the
remaining path components via :func:`getattr`. Module-level
functions resolve in one ``getattr`` step; bound methods resolve in
two (class lookup, then unbound method); the dispatcher binds the
method against the connector instance the registry returns.

**Forbidden handler shapes** -- the helper rejects with
:class:`HandlerRefError`:

* **Closures / inner functions** -- ``__qualname__`` contains
  ``<locals>`` (Python's marker for any function defined inside
  another function). The dispatcher cannot reconstruct the
  enclosing scope's free variables from a dotted path.
* **Lambdas** -- ``__qualname__`` is ``"<lambda>"``. Same root
  cause as closures, plus the dispatcher can't introspect
  parameter intent.
* **Functools.partial wrappers** -- no ``__module__`` /
  ``__qualname__`` of their own; the wrapped target lives at
  ``.func`` but binding partial args at registration time defeats
  the dispatcher's parameter-validation contract.
* **Non-coroutine functions** -- typed ops must be ``async def``;
  the dispatcher always ``await``s the resolved callable.

The rejection happens at registration time, not dispatch time, so
operator-visible failures surface at app startup (lifespan crash on
the first connector init) rather than at first request -- the
fail-fast deployment shape the chassis tasks already established.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._handler_resolve import import_handler
from meho_backplane.operations.embed import (
    build_embedding_text,
    compute_embedding_text_hash,
    encode_endpoint_text,
)
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "CompositeOpHandler",
    "HandlerRefError",
    "HandlerSignatureError",
    "TypedOpHandler",
    "clear_typed_op_registrars",
    "derive_handler_ref",
    "register_composite_operation",
    "register_typed_op_registrar",
    "register_typed_operation",
    "run_typed_op_registrars",
    "validate_composite_handler_signature",
]


#: Type alias for the no-arg async registrar callables connector
#: subpackages append at import time via
#: :func:`register_typed_op_registrar`. Each callable runs
#: :func:`register_typed_operation` for every typed op the connector
#: ships. Keyword args (e.g. ``embedding_service``) are passed by the
#: runner so tests can inject stubs without rebinding the registrar
#: itself.
type TypedOpRegistrar = Callable[..., Awaitable[None]]


# Module-scope registrar list. Connector subpackages append at import
# time (from their ``__init__.py``); the FastAPI lifespan
# (``meho_backplane.main.lifespan``) calls
# :func:`run_typed_op_registrars` after
# :func:`~meho_backplane.connectors.registry._eager_import_connectors`
# has walked the subpackages. The two-phase shape (sync import-time
# registration of v2 connector classes + async lifespan-time
# registration of typed ops) keeps every ``__init__.py`` sync while
# letting the typed-op upserts await on the DB + the embedding
# pipeline. The list is preserved across tests by default; the
# autouse fixture in ``tests/conftest.py`` only resets it when the
# test file explicitly drives the lifespan (e.g. via TestClient).
_TYPED_OP_REGISTRARS: list[TypedOpRegistrar] = []


def register_typed_op_registrar(registrar: TypedOpRegistrar) -> None:
    """Append *registrar* to the module-level typed-op registrar list.

    Called once per connector subpackage at Python module-import time
    so the lifespan-driven runner can iterate every shipped connector
    without explicit per-connector wiring in
    :mod:`meho_backplane.main`. The registrar itself is an async
    callable that runs :func:`register_typed_operation` for every op
    the connector exposes.

    Idempotency at runtime: the runner tolerates the same registrar
    appearing twice (it would just upsert the same row twice, which
    is itself a no-op for the embedding pipeline). The
    test-isolation fixture in ``tests/conftest.py`` clears the list
    between modules that drive the lifespan via TestClient so
    duplicate appends from re-imports don't accumulate.
    """
    _TYPED_OP_REGISTRARS.append(registrar)


def clear_typed_op_registrars() -> None:
    """Empty the registrar list. Test-only.

    Production code never calls this; the registrar list is a
    one-shot append at module-import time, and the lifespan is the
    only consumer. Tests that mock the lifespan (or that exercise
    the registrar runner in isolation) use this hook to start each
    test from an empty list.
    """
    _TYPED_OP_REGISTRARS.clear()


async def run_typed_op_registrars(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Invoke every registered typed-op registrar in registration order.

    Called from the FastAPI lifespan after
    :func:`~meho_backplane.connectors.registry._eager_import_connectors`
    so every shipped connector has self-registered its registrar by
    the time the runner iterates. Registrars run sequentially — the
    embedding pipeline is single-threaded per process, and the
    descriptor upserts are quick (one DB round-trip per op on the
    skip-re-embed path) so parallelism would buy nothing.

    A failure in one registrar **propagates**: a connector that
    can't upsert its typed-op rows is a deploy bug, not a runtime
    condition, and should surface as a lifespan crash so the
    operator sees the CrashLoopBackOff rather than a quietly-broken
    dispatch. Operators reading the traceback see the connector's
    own module path in the registrar identity, which points at the
    failing op directly.

    The ``embedding_service`` parameter is the test seam: production
    callers leave it ``None`` and each registrar resolves the
    process-wide singleton via the
    :func:`register_typed_operation` body; test callers (chassis
    integration tests) inject a stub so the suite doesn't pull the
    ONNX model on every run.
    """
    log = structlog.get_logger(__name__)
    for registrar in _TYPED_OP_REGISTRARS:
        log.info(
            "typed_op_registrar_running",
            registrar=f"{registrar.__module__}.{registrar.__qualname__}",
        )
        await registrar(embedding_service=embedding_service)


#: Type alias for typed-op handlers -- async callables returning a
#: result dict. The dispatcher binds the connector instance for
#: bound-method handlers, so the runtime signature seen by the
#: dispatcher post-binding is ``(target, params) -> Awaitable[dict]``;
#: the registration helper only inspects the bare callable's identity
#: and asyncness, not its parameter shape (the dispatcher validates
#: incoming params against ``parameter_schema`` at dispatch time).
type TypedOpHandler = Callable[..., Awaitable[dict[str, Any]]]


#: Type alias for composite-op handlers. The dispatcher's composite
#: branch (:func:`~meho_backplane.operations._branches.dispatch_composite`)
#: invokes the handler with keyword args ``operator``, ``target``,
#: ``params``, and ``dispatch_child`` -- the last is a
#: :class:`~meho_backplane.operations.composite.DispatchChild` callable
#: the handler uses to recurse into sub-ops. The alias is permissive
#: (``Callable[..., Awaitable[dict]]``) for the same reason
#: :data:`TypedOpHandler` is: connector-bound methods carry a leading
#: ``self`` that disappears at bind time. The shape contract is
#: enforced at registration time by
#: :func:`validate_composite_handler_signature`, which asserts the
#: handler exposes a ``dispatch_child`` parameter -- the only positional
#: distinction from typed handlers.
type CompositeOpHandler = Callable[..., Awaitable[dict[str, Any]]]


# Bounded enum for ``safety_level`` -- mirrors the DB CHECK constraint
# on :attr:`EndpointDescriptor.safety_level` so the helper rejects
# invalid values at the Python boundary rather than at commit time
# (where the IntegrityError message names the constraint, not the
# field that produced it).
_VALID_SAFETY_LEVELS: frozenset[str] = frozenset({"safe", "caution", "dangerous"})


class HandlerRefError(ValueError):
    """Raised when a typed handler's identity cannot be serialised to a dotted path.

    Inherits from :class:`ValueError` so existing ``except ValueError``
    blocks in connector init paths catch the failure without needing a
    targeted ``except HandlerRefError``. The targeted class still exists
    so tests can assert on the precise shape, and so operators reading
    a startup traceback see the class name that explains the failure
    mode immediately.
    """


class HandlerSignatureError(ValueError):
    """Raised when a typed/composite handler's parameter shape contradicts its registration.

    Symmetric cross-rejection: typed handlers (no ``dispatch_child``)
    registered via :func:`register_composite_operation` raise this with
    a "must accept 'dispatch_child' parameter" message; composite handlers
    (with ``dispatch_child``) registered via :func:`register_typed_operation`
    raise this with a "register via register_composite_operation instead"
    message. Both cases name the handler's dotted path so the operator
    can locate the misroute without grepping for the signature.

    The check runs at registration time -- not first dispatch -- so the
    failure surfaces during connector init (lifespan crash, operator
    sees the dotted path immediately) rather than as a
    :class:`TypeError` raised by the dispatcher branch when a handler
    is missing the expected keyword. The fail-fast deployment shape
    matches :class:`HandlerRefError`.

    Subclass of :class:`ValueError` so existing ``except ValueError``
    blocks in connector init paths catch it without a targeted
    ``except HandlerSignatureError``.
    """


def derive_handler_ref(handler: TypedOpHandler) -> str:
    """Derive the dotted Python path the dispatcher will import at dispatch time.

    Module-level functions resolve to ``f"{module}.{qualname}"`` --
    e.g. ``"meho_backplane.connectors.vault.ops.vault_kv_read"``.
    Bound methods resolve to ``f"{module}.{Class.method}"`` -- e.g.
    ``"meho_backplane.connectors.vault.connector.VaultConnector.kv_read"``
    -- because Python's ``__qualname__`` on a bound method already
    includes the class name, which the dispatcher's chained
    :func:`getattr` walk consumes.

    Rejection rules (see module docstring for the full rationale):

    * ``inspect.iscoroutinefunction(handler)`` must be true -- typed
      ops are async; the dispatcher always ``await``s the resolved
      callable.
    * ``__qualname__`` must not contain ``<locals>`` -- closures and
      inner functions cannot round-trip through the dispatcher's
      ``importlib`` resolution.
    * ``__qualname__`` must not be ``<lambda>`` -- lambdas have no
      stable dotted path and no introspectable parameter intent.
    * The handler must expose ``__module__`` and ``__qualname__`` --
      :class:`functools.partial` wrappers (and similar callable
      objects without these attrs) are rejected by the missing-attr
      :class:`HandlerRefError`.

    For bound methods the underlying ``__func__`` is what
    :func:`inspect.iscoroutinefunction` checks; we accept either
    pure async functions or coroutine-returning bound methods.
    """
    qualname = getattr(handler, "__qualname__", None)
    module = getattr(handler, "__module__", None)
    if not qualname or not module:
        raise HandlerRefError(
            "typed handlers must expose __module__ and __qualname__ "
            "(received an object missing one of them -- functools.partial "
            "wrappers and similar callables are not supported)"
        )
    # Lambda check before the closure check: a lambda defined inside
    # a function body has ``__qualname__`` like ``"outer.<locals>.<lambda>"``
    # -- both conditions match. The lambda message is the more
    # actionable one for the operator (it points at the literal
    # ``lambda x: ...`` syntax rather than at scope rules), so it wins.
    if qualname == "<lambda>" or qualname.endswith(".<lambda>"):
        raise HandlerRefError(
            "typed handlers must be named module-level functions or bound methods, not lambdas"
        )
    if "<locals>" in qualname:
        raise HandlerRefError("typed handlers must be module-level or bound methods, not closures")
    # ``iscoroutinefunction`` returns True for bound methods whose
    # underlying ``__func__`` is ``async def`` -- which is the
    # connector-handler shape we want.
    if not inspect.iscoroutinefunction(handler):
        raise HandlerRefError(
            f"typed handler {module}.{qualname} must be an async def "
            "(coroutine function); the dispatcher always awaits the "
            "resolved callable"
        )
    return f"{module}.{qualname}"


def _handler_parameter_names(handler: Any) -> list[str]:
    """Return the handler's parameter names with a leading ``self`` dropped.

    Mirrors the introspection
    :func:`~meho_backplane.operations._branches.dispatch_typed` runs at
    dispatch time -- ``inspect.signature(handler).parameters`` then drop
    a leading ``self`` so unbound-method handlers and bound methods that
    weren't rebound report the same shape. The bound-method case (the
    typical typed-connector ``self.kv_read`` pattern) has ``__self__``
    already absorbed before this helper sees the callable, so ``self``
    is only present on unbound methods.
    """
    sig = inspect.signature(handler)
    names = list(sig.parameters.keys())
    if names and names[0] == "self":
        names = names[1:]
    return names


def validate_composite_handler_signature(handler: Any) -> None:
    """Assert *handler* accepts a ``dispatch_child`` parameter.

    Composite handlers receive
    ``dispatch_child: DispatchChild`` from the dispatcher at invocation
    time
    (:func:`~meho_backplane.operations._branches.dispatch_composite`).
    Registering a handler without it would surface the failure as a
    :exc:`TypeError` at first dispatch -- late, with poor signal.
    Checking the signature at registration time fails fast with an
    operator-readable message and the handler's dotted path.

    Raises
    ------
    HandlerSignatureError
        Handler's parameters do not include ``dispatch_child``.
    """
    param_names = _handler_parameter_names(handler)
    if "dispatch_child" not in param_names:
        module = getattr(handler, "__module__", "<unknown>")
        qualname = getattr(handler, "__qualname__", repr(handler))
        raise HandlerSignatureError(
            f"composite handler {module}.{qualname} "
            f"must accept a 'dispatch_child' parameter "
            f"(per meho_backplane.operations.composite.DispatchChild); "
            f"signature is ({', '.join(param_names)})"
        )


def _assert_handler_ref_resolvable(
    handler_ref: str,
    *,
    op_id: str,
    product: str,
    version: str,
    impl_id: str,
) -> None:
    """Fail-closed: ``handler_ref`` MUST resolve via the dispatcher's import walk.

    The dispatcher resolves ``handler_ref`` at dispatch time via
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    (``importlib.import_module`` + chained :func:`getattr`). A connector
    that registers an op whose ``handler_ref`` cannot resolve produces
    a ``handler_unreachable`` error at first dispatch -- which closed
    the bind9 G3.4 Initiative #367 green-but-hollow (#697): the
    integration lane was advisory, the per-op meta-tool test failed,
    and the Initiative closed anyway. #699 fixed the *binding* layer
    (MRO-aware :func:`is_unbound_method`); this guard fills the
    parallel #697 AC #3 line at the *resolvability* layer so a future
    connector cannot ship green with an unreachable handler_ref.

    Catches at registration time:

    * Typos / stale entries -- handler removed but registrar entry
      still references the dotted path.
    * Module path changes ``derive_handler_ref``'s ``{module}.{qualname}``
      shape doesn't anticipate (submodule re-exports, decorator wrapping).
    * Non-callable resolution -- e.g. a refactor that replaces an
      ``async def`` with a class attribute.

    Does **not** catch the precise #697 root cause (the resolver's
    MRO-aware unbound-method binding -- that's a runtime-binding
    question upstream of resolution). #699 fixed that at the binding
    layer; this guard is the parallel registration-time backstop.

    Raises
    ------
    HandlerRefError
        ``handler_ref`` does not resolve to a callable via
        :func:`import_handler`. Subclass of :class:`ValueError`; the
        registrar surfaces the original :exc:`ImportError` /
        :exc:`TypeError` as ``__cause__``.
    """
    try:
        import_handler(handler_ref)
    except (ImportError, TypeError) as exc:
        raise HandlerRefError(
            f"register_typed_operation: handler_ref {handler_ref!r} "
            f"(op_id={op_id!r}, product={product!r}, version={version!r}, "
            f"impl_id={impl_id!r}) does not resolve to a callable via "
            f"import_handler -- the dispatcher would surface this as "
            f"handler_unreachable at dispatch. Fail-closed at "
            f"registration so the connector cannot ship green with an "
            f"unreachable handler (#697 / #699)."
        ) from exc


def _reject_composite_handler(handler: Any) -> None:
    """Symmetric: typed registrations reject composite-shaped handlers.

    Run from :func:`register_typed_operation` so a misrouted composite
    surfaces at registration time. Inverse of
    :func:`validate_composite_handler_signature`. A handler with a
    ``dispatch_child`` parameter is a composite by signature; the typed
    branch would never pass that arg in, so the handler would crash on
    first dispatch with a confusing :exc:`TypeError`. Rejecting at
    registration produces a precise error pointing at the right helper.

    Raises
    ------
    HandlerSignatureError
        Handler exposes a ``dispatch_child`` parameter (composite shape).
    """
    param_names = _handler_parameter_names(handler)
    if "dispatch_child" in param_names:
        module = getattr(handler, "__module__", "<unknown>")
        qualname = getattr(handler, "__qualname__", repr(handler))
        raise HandlerSignatureError(
            f"typed registration of {module}.{qualname} rejected: "
            f"handler accepts 'dispatch_child' -- "
            f"register via register_composite_operation() instead"
        )


def _validate_op_id(op_id: str) -> str:
    """Validate ``op_id`` is a non-empty, non-whitespace string.

    The DB column is ``Text NOT NULL`` but the SQL layer would let
    ``""`` or ``"   "`` through; both would defeat the natural-key
    lookup the dispatcher runs (``op_id`` would not collide with a
    real op but ``search_operations``' BM25 ranker would return
    nonsense for it). Reject at the Python boundary.
    """
    if not isinstance(op_id, str) or not op_id.strip():
        raise ValueError(f"op_id must be a non-empty, non-whitespace string (received {op_id!r})")
    return op_id


def _validate_safety_level(safety_level: str) -> str:
    """Validate ``safety_level`` is one of the bounded enum values.

    Mirrors the DB CHECK constraint on
    :attr:`EndpointDescriptor.safety_level` so the helper rejects
    invalid values at the Python boundary rather than at commit time
    (where the :class:`IntegrityError` message names the constraint,
    not the field that produced it).
    """
    if safety_level not in _VALID_SAFETY_LEVELS:
        raise ValueError(
            f"safety_level must be one of {sorted(_VALID_SAFETY_LEVELS)} "
            f"(received {safety_level!r})"
        )
    return safety_level


def _validate_when_to_use_pairing(group_key: str | None, when_to_use: str | None) -> str | None:
    """Validate the ``group_key`` / ``when_to_use`` pairing at the boundary.

    Pairing rules (G0.9-T4a #731):

    * ``group_key`` is set -> ``when_to_use`` MUST be a non-empty,
      non-whitespace string. Empty / whitespace-only / ``None`` raises
      :class:`ValueError`. This catches the original Signal #5 failure
      mode: the auto-derive default at the
      ``operation_group.when_to_use`` column used to swallow any
      missing-blurb call as a generic ``"Operations grouped under …"``
      placeholder; killing the default at this boundary makes a
      missing curation impossible to ship silently.
    * ``group_key`` is ``None`` -> ``when_to_use`` MUST be ``None`` or
      an empty / whitespace-only string. Empty strings are normalised
      to ``None`` here (returned by this helper) to keep the contract
      symmetrical: callers cycling through ``spec.when_to_use or None``
      see the same shape, and any meaningful blurb on an ungrouped op
      is rejected (the prose would be persisted nowhere since no
      :class:`OperationGroup` row exists to attach it to).

    The signature-level contract (the public helpers' kwarg-required
    shape) raises :class:`TypeError` on entirely-omitted kwargs;
    this helper covers the runtime-paired case.

    Returns
    -------
    str | None
        The validated (and, when ``group_key is None``, normalised)
        ``when_to_use`` value. Callers should reassign:
        ``when_to_use = _validate_when_to_use_pairing(group_key, when_to_use)``.
    """
    if group_key is not None:
        if not isinstance(when_to_use, str) or not when_to_use.strip():
            raise ValueError(
                "when_to_use must be a non-empty, non-whitespace string when "
                f"group_key is set (group_key={group_key!r}, "
                f"when_to_use={when_to_use!r})"
            )
        return when_to_use
    # group_key is None: normalise empty / whitespace-only strings to None
    # so the contract stays symmetric with the spec.when_to_use or None
    # idiom; reject any meaningful non-empty blurb.
    if when_to_use is None:
        return None
    if not isinstance(when_to_use, str):
        raise ValueError(
            "when_to_use must be None when group_key is None (no operation "
            f"group row exists to attach the blurb to; received "
            f"when_to_use={when_to_use!r})"
        )
    if not when_to_use.strip():
        return None
    raise ValueError(
        "when_to_use must be None when group_key is None (no operation "
        f"group row exists to attach the blurb to; received "
        f"when_to_use={when_to_use!r})"
    )


async def _resolve_or_create_group(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    group_key: str,
    when_to_use: str,
    now: datetime,
) -> uuid.UUID:
    """Look up or create an :class:`OperationGroup` row for a built-in/global group.

    Typed-connector groups are always ``tenant_id IS NULL`` -- the
    connector ships them as part of the built-in surface, not a
    tenant-curated curation. ``review_status='enabled'`` because the
    operator-review queue (G0.7) doesn't gate typed registrations:
    the typed connector author already vouched for the group at code
    review time.

    Auto-derivation of ``name`` from ``group_key`` is intentionally
    minimal -- a humanised title-case of the dotted key. ``when_to_use``
    is **caller-supplied** (G0.9-T4a #731): every connector must hand
    in a curated blurb so the operator-facing
    ``list_operation_groups`` (T8) surface never shows the legacy
    auto-derive placeholder. The previous default
    (``"Operations grouped under '<key>' for <product> <impl>."``) was
    the Signal #5 cause from the 2026-05-20 RDC dogfood; killing the
    default at this boundary makes the structural gap impossible to
    silently re-introduce.

    Concurrency note: two concurrent connectors registering against
    the same ``group_key`` race here. The partial unique index
    ``operation_group_global_idx`` (migration ``0005``) catches the
    race at flush time; the loser sees :class:`IntegrityError` and
    re-reads the row. v0.2 connector init is single-threaded per pod
    (lifespan startup runs registrations sequentially), so the race
    is a theoretical concern rather than an observed one -- the
    retry shim lives at the caller (G3 connector packages can wrap
    their init in their own retry) rather than here.

    First-register vs. existing-row update: on a row that already
    exists, the helper returns the persisted id unchanged. The
    caller-supplied ``when_to_use`` is **not** written back to an
    existing row -- updating curated copy belongs to a separate
    seeding/admin path, not the per-op registration loop. The
    same-process re-register case (lifespan restart) sees the
    existing row and skips the write.
    """
    result = await session.execute(
        select(OperationGroup).where(
            OperationGroup.tenant_id.is_(None),
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
            OperationGroup.group_key == group_key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id

    # Title-case the dotted key for the default name -- ``"vm-lifecycle"``
    # -> ``"Vm Lifecycle"``, ``"kv"`` -> ``"Kv"``. The result is
    # operator-readable enough as a placeholder; connectors that care
    # about presentation override the row out-of-band.
    name = group_key.replace("-", " ").replace("_", " ").replace(".", " ").title()
    group = OperationGroup(
        id=uuid.uuid4(),
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        group_key=group_key,
        name=name,
        when_to_use=when_to_use,
        review_status="enabled",
        created_at=now,
        updated_at=now,
    )
    session.add(group)
    # Flush so ``group.id`` is populated for the descriptor's FK.
    await session.flush()
    return group.id


# Pre-existing G0.6-T4 #395 public-API helper; G0.9-T4a #731 adds only the
# ``when_to_use`` kwarg + docstring section + one pairing-validator call.
# Splitting kwargs validation / session-owned vs. caller-owned dispatch
# into helpers is deferred so this signature-only change stays minimally
# invasive. code-quality-allow: pre-existing pre-G0.9 function length
async def register_typed_operation(
    *,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    handler: TypedOpHandler,
    summary: str,
    description: str,
    parameter_schema: dict[str, Any],
    when_to_use: str | None,
    response_schema: dict[str, Any] | None = None,
    group_key: str | None = None,
    tags: list[str] | None = None,
    safety_level: Literal["safe", "caution", "dangerous"] = "safe",
    requires_approval: bool = False,
    llm_instructions: dict[str, Any] | None = None,
    custom_description: str | None = None,
    session: AsyncSession | None = None,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert a typed operation into ``endpoint_descriptor``. Skip re-embed on unchanged text.

    Parameters
    ----------
    product, version, impl_id, op_id
        Natural-key coordinates. ``op_id`` must be a non-empty
        non-whitespace string; the validation runs at the Python
        boundary so the failure mode is :class:`ValueError` with a
        field-named message rather than a DB-side
        :class:`IntegrityError` further down the stack.
    handler
        The async callable the dispatcher will route to at dispatch
        time. Must be a module-level function or a bound method --
        closures, lambdas, and :class:`functools.partial` wrappers
        are rejected with :class:`HandlerRefError` (a
        :class:`ValueError` subclass). The dotted path is derived
        from ``handler.__module__`` + ``handler.__qualname__``.
    summary, description
        Operator-facing prose. The two columns feed the BM25 half of
        :func:`~meho_backplane.operations.search_operations` (G0.6-T6,
        not yet shipped) and -- combined with ``custom_description``
        and ``tags`` -- form the embedding text. Quality matters
        operationally: a vague summary degrades retrieval ranking.
    parameter_schema, response_schema
        JSON Schema 2020-12 documents. The dispatcher (T5) validates
        inbound params against ``parameter_schema`` before routing;
        ``response_schema`` is informational in v0.2.
    when_to_use
        **Required** kwarg (no default, since G0.9-T4a #731). Curated
        prose answering *"which group should I search for the
        question I have?"*; persisted onto the
        :class:`OperationGroup` row when the helper has to create one.
        Pairing rules:

        * ``group_key`` set -> ``when_to_use`` must be a non-empty,
          non-whitespace string.
        * ``group_key=None`` -> ``when_to_use`` must be ``None`` (no
          group row exists to attach the blurb to).

        The previous auto-derive default
        (``"Operations grouped under '<key>' for <product> <impl>."``)
        was Signal #5 from the 2026-05-20 RDC dogfood -- removing it
        forces every connector author to ship a real blurb. T4b #732
        replaces the placeholder strings T4a inserts at the existing
        call sites with curated content.
    group_key
        Optional grouping handle (e.g. ``"vm-lifecycle"``, ``"kv"``).
        Resolved to an existing :class:`OperationGroup` row or a new
        one created with ``review_status='enabled'``. ``None`` leaves
        ``group_id`` NULL (ungrouped op).
    when_to_use
        Optional curated agent-actionable prose explaining *when to
        pick this group* over the other groups the same connector
        exposes -- the question an LLM client tries to answer before
        calling ``search_operations`` for a specific op. Surfaced
        verbatim by ``list_operation_groups`` (T8). When supplied on
        the first registration into a group, it's written to the new
        :class:`OperationGroup` row's ``when_to_use`` column; on
        subsequent registrations into an existing group, the helper
        UPDATEs the row only when the string differs (so curation
        edits in a code-only PR like T4b #732 land on restart).
        ``None`` falls back to the auto-derive default the row's
        first writer used (kept during the T4b → T4a #731 transition;
        T4a removes the fallback and makes the kwarg required).
    tags
        Optional list of short keyword tags (e.g.
        ``["read-only", "cluster"]``). Part of the embedding text;
        empty list and ``None`` are equivalent for the embedding
        computation but the column is NOT NULL with default ``[]``.
    safety_level
        ``"safe"`` (default), ``"caution"``, or ``"dangerous"``. The
        policy gate consumes this; safe ops execute under the
        default-allow policy.
    requires_approval
        When ``True``, the dispatcher writes an audit row in
        ``status='pending'`` and waits for operator decision before
        executing. Independent of ``safety_level``.
    llm_instructions
        Optional structured agent guidance ("when to call",
        "parameter hints", "output format"). The agent prompt
        construction in the meta-tools (T8) inlines this verbatim
        when the LLM is choosing the op.
    custom_description
        Operator-authored override applied at G0.7 ingest-review
        time. For typed registrations this is always ``None`` from
        the connector itself -- typed connectors don't have a review
        queue -- but the column carries the same shape so a future
        operator-admin UI can edit it without a schema change.
    session
        Optional caller-owned :class:`AsyncSession`. When provided
        the helper does **not** commit -- the caller controls
        transaction boundaries. When ``None`` the helper opens its
        own session, commits, and closes.
    embedding_service
        Optional caller-supplied :class:`EmbeddingService`. Test
        seam; production callers leave ``None`` so the helper
        resolves the process-wide singleton.

    Returns
    -------
    None
        The helper is fire-and-forget at the call site -- connector
        init code calls it for every op it exposes and doesn't read
        the result. The dispatcher reads the table directly.

    Raises
    ------
    TypeError
        ``when_to_use`` omitted entirely (signature-level required
        kwarg; the no-default contract is the structural fix from
        G0.9-T4a #731).
    ValueError
        ``op_id`` empty / whitespace, ``safety_level`` not in the
        bounded enum, or the ``group_key`` / ``when_to_use`` pairing
        contract is violated (see ``when_to_use`` param description).
    HandlerRefError
        Handler is a closure, lambda, partial, or non-coroutine
        function. Subclass of :class:`ValueError`.
    HandlerSignatureError
        The natural key is already registered with
        ``source_kind="composite"``. Cross-kind re-registration is
        rejected at lookup time so a dispatch-time :exc:`TypeError`
        cannot surface from an inconsistent persisted row. Subclass
        of :class:`ValueError`.

    Behavioural contract
    --------------------

    * **First call** for a given natural key -- inserts a new row,
      computes the embedding, populates every field including
      ``handler_ref`` derived from ``handler.__module__`` +
      ``handler.__qualname__``.
    * **Re-call with the same embedding text** (summary +
      description + custom_description + tags unchanged) -- skips
      the embedding compute, UPDATEs non-embedding fields when
      changed, advances ``updated_at``. The hash comparison is
      against the persisted row's text, not a stored column --
      ``endpoint_descriptor`` has no ``body_hash`` column in v0.2
      (deferred to a future migration if measurements show the
      re-compose cost matters at scale).
    * **Re-call with changed embedding text** -- recomputes the
      embedding, UPDATEs every field plus ``embedding``, advances
      ``updated_at``.
    * **source_kind always 'typed'**, ``tenant_id always None`` --
      typed registrations are built-in / global by construction.
    """
    _validate_op_id(op_id)
    _validate_safety_level(safety_level)
    when_to_use = _validate_when_to_use_pairing(group_key, when_to_use)
    _reject_composite_handler(handler)
    handler_ref = derive_handler_ref(handler)
    _assert_handler_ref_resolvable(
        handler_ref,
        op_id=op_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )

    tags_list = list(tags) if tags is not None else []

    if session is not None:
        await _register_in_session(
            session,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            source_kind="typed",
            handler_ref=handler_ref,
            summary=summary,
            description=description,
            parameter_schema=parameter_schema,
            response_schema=response_schema,
            group_key=group_key,
            when_to_use=when_to_use,
            tags_list=tags_list,
            safety_level=safety_level,
            requires_approval=requires_approval,
            llm_instructions=llm_instructions,
            custom_description=custom_description,
            embedding_service=embedding_service,
            commit=False,
        )
        return

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        await _register_in_session(
            owned_session,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            source_kind="typed",
            handler_ref=handler_ref,
            summary=summary,
            description=description,
            parameter_schema=parameter_schema,
            response_schema=response_schema,
            group_key=group_key,
            when_to_use=when_to_use,
            tags_list=tags_list,
            safety_level=safety_level,
            requires_approval=requires_approval,
            llm_instructions=llm_instructions,
            custom_description=custom_description,
            embedding_service=embedding_service,
            commit=True,
        )


# Pre-existing G3.1-T4 #504 public-API helper; G0.9-T4a #731 mirrors the
# typed-helper change (``when_to_use`` kwarg + docstring section + one
# pairing-validator call). Refactor is the same scope-deferred call as
# :func:`register_typed_operation` above. code-quality-allow: pre-G0.9 length
async def register_composite_operation(
    *,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    handler: CompositeOpHandler,
    summary: str,
    description: str,
    parameter_schema: dict[str, Any],
    when_to_use: str | None,
    response_schema: dict[str, Any] | None = None,
    group_key: str | None = None,
    tags: list[str] | None = None,
    safety_level: Literal["safe", "caution", "dangerous"] = "dangerous",
    requires_approval: bool = True,
    llm_instructions: dict[str, Any] | None = None,
    custom_description: str | None = None,
    session: AsyncSession | None = None,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert a composite operation into ``endpoint_descriptor``.

    Sibling of :func:`register_typed_operation`. The two helpers share
    one private upsert path (:func:`_register_in_session`) -- they
    differ in (a) the column they write to ``source_kind`` (this one
    writes ``"composite"``), (b) the handler-signature contract they
    enforce (this one rejects handlers without ``dispatch_child``),
    and (c) the policy defaults (this one defaults
    ``safety_level="dangerous"`` and ``requires_approval=True``).

    Parameters
    ----------
    product, version, impl_id, op_id
        Natural-key coordinates. Same shape as
        :func:`register_typed_operation`. ``op_id`` is the dotted
        identifier composites are surfaced under -- by convention
        ``"<product>.composite.<verb>.<noun>"`` (e.g.
        ``"vmware.composite.vm.create"``); the dispatcher treats it
        as opaque.
    handler
        The async callable the dispatcher routes the composite to.
        Must be a module-level function or bound method (closure /
        lambda / :class:`functools.partial` rejected via
        :func:`derive_handler_ref`'s contract -- shared with the typed
        helper). The handler MUST accept a ``dispatch_child``
        parameter; the dispatcher's composite branch
        (:func:`~meho_backplane.operations._branches.dispatch_composite`)
        passes a :class:`~meho_backplane.operations.composite.DispatchChild`
        callable in by keyword. Handlers missing the parameter raise
        :class:`HandlerSignatureError` at registration time -- not
        first dispatch -- so the failure surfaces in lifespan.
    summary, description, parameter_schema, response_schema, group_key, when_to_use, tags
        Identical to :func:`register_typed_operation`.
    when_to_use
        Identical to :func:`register_typed_operation` -- **required**
        kwarg (no default, since G0.9-T4a #731), paired with
        ``group_key`` (set both or pass both as ``None``).
    safety_level
        Defaults to ``"dangerous"`` (vs. typed's ``"safe"`` default).
        Composites typically orchestrate write ops -- VM lifecycle,
        cluster patching, host evacuation -- and the safe-by-default
        for the operator is the assume-dangerous posture. Per-op
        overrides at the call site for read-only composites
        (``vmware.composite.vm.info``,
        ``vmware.composite.performance.summary``).
    requires_approval
        Defaults to ``True`` (vs. typed's ``False`` default). Same
        rationale: composites' blast radius is larger by construction
        (one composite call ⇒ N sub-ops dispatched), so the policy
        gate's approval-queue path is the right default. Read-only
        composites override at the call site.
    llm_instructions, custom_description, session, embedding_service
        Identical to :func:`register_typed_operation`.

    Returns
    -------
    None
        Fire-and-forget at the call site. The dispatcher reads the
        table directly.

    Raises
    ------
    TypeError
        ``when_to_use`` omitted entirely (signature-level required
        kwarg from G0.9-T4a #731).
    ValueError
        ``op_id`` empty / whitespace, ``safety_level`` not in the
        bounded enum, or the ``group_key`` / ``when_to_use`` pairing
        contract is violated (see :func:`register_typed_operation`).
    HandlerSignatureError
        Handler does not accept a ``dispatch_child`` parameter, **or**
        the natural key is already registered with
        ``source_kind="typed"`` -- cross-kind re-registration is
        rejected at lookup time so a dispatch-time :exc:`TypeError`
        cannot surface from an inconsistent persisted row. Subclass
        of :class:`ValueError`.
    HandlerRefError
        Handler is a closure, lambda, partial, or non-coroutine
        function (inherited from
        :func:`derive_handler_ref`'s contract). Subclass of
        :class:`ValueError`.

    Behavioural contract
    --------------------

    Identical to :func:`register_typed_operation`'s contract -- the
    private upsert path is the same code -- with two differences:

    * The persisted row carries ``source_kind="composite"``, which
      routes the dispatcher to its composite branch at dispatch time
      (the branch that builds a :class:`DispatchChild` callable and
      passes it as ``dispatch_child=`` to the handler).
    * The signature-validation step runs
      :func:`validate_composite_handler_signature` instead of
      :func:`_reject_composite_handler`, so the cross-rejection is
      symmetric.

    The composite handler's ``dispatch_child`` calls land in the
    audit table as recursive child rows linked to the parent
    composite's audit row via ``audit_log.parent_audit_id`` (the
    G0.6-T7 #398 contract); the per-task ``composite_depth_var``
    contextvar enforces the recursion cap
    (:attr:`Settings.composite_max_depth`, default 8).
    """
    _validate_op_id(op_id)
    _validate_safety_level(safety_level)
    when_to_use = _validate_when_to_use_pairing(group_key, when_to_use)
    validate_composite_handler_signature(handler)
    handler_ref = derive_handler_ref(handler)
    _assert_handler_ref_resolvable(
        handler_ref,
        op_id=op_id,
        product=product,
        version=version,
        impl_id=impl_id,
    )

    tags_list = list(tags) if tags is not None else []

    if session is not None:
        await _register_in_session(
            session,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            source_kind="composite",
            handler_ref=handler_ref,
            summary=summary,
            description=description,
            parameter_schema=parameter_schema,
            response_schema=response_schema,
            group_key=group_key,
            when_to_use=when_to_use,
            tags_list=tags_list,
            safety_level=safety_level,
            requires_approval=requires_approval,
            llm_instructions=llm_instructions,
            custom_description=custom_description,
            embedding_service=embedding_service,
            commit=False,
        )
        return

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as owned_session:
        await _register_in_session(
            owned_session,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
            source_kind="composite",
            handler_ref=handler_ref,
            summary=summary,
            description=description,
            parameter_schema=parameter_schema,
            response_schema=response_schema,
            group_key=group_key,
            when_to_use=when_to_use,
            tags_list=tags_list,
            safety_level=safety_level,
            requires_approval=requires_approval,
            llm_instructions=llm_instructions,
            custom_description=custom_description,
            embedding_service=embedding_service,
            commit=True,
        )


# Pre-existing G0.6-T4 #395 shared upsert path (typed + composite branches
# converge here); G0.9-T4a #731 threads one ``when_to_use`` kwarg into the
# group-resolve call. Refactor of the first-register / skip-re-embed /
# re-embed branches into helpers is a separate Task.
# code-quality-allow: pre-G0.9 function length
async def _register_in_session(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    source_kind: Literal["typed", "composite"],
    handler_ref: str,
    summary: str,
    description: str,
    parameter_schema: dict[str, Any],
    response_schema: dict[str, Any] | None,
    group_key: str | None,
    when_to_use: str | None,
    tags_list: list[str],
    safety_level: str,
    requires_approval: bool,
    llm_instructions: dict[str, Any] | None,
    custom_description: str | None,
    embedding_service: EmbeddingService | None,
    commit: bool,
) -> None:
    """Inner implementation -- run the upsert logic against *session*.

    Split out so the public :func:`register_typed_operation` and
    :func:`register_composite_operation` share the upsert path
    without duplicating it; ``source_kind`` is the only column whose
    value depends on which entry point the caller used (the helpers'
    public surfaces differ in what they validate, not in what they
    write -- the shared upsert is the right factoring). The ``commit``
    flag controls whether to issue the final commit; flush always runs
    so ORM-side defaults (``id``, ``created_at``, ``updated_at``) are
    populated even when the caller defers the commit.
    """
    log = structlog.get_logger()
    now = datetime.now(UTC)

    # Compose the embedding text + hash from the incoming args; the
    # same composer applies to the persisted row's text below for
    # the change-detection comparison.
    incoming_text = build_embedding_text(
        summary=summary,
        description=description,
        custom_description=custom_description,
        tags=tags_list,
    )
    incoming_hash = compute_embedding_text_hash(incoming_text)

    # Resolve / create the group up-front so the descriptor INSERT/
    # UPDATE has the FK target id ready. ``group_key=None`` leaves
    # ``group_id`` NULL -- an ungrouped op is dispatchable; the
    # admin UI can group it later.
    group_id: uuid.UUID | None = None
    if group_key is not None:
        # Pairing was validated at the public-helper boundary
        # (:func:`_validate_when_to_use_pairing`); ``when_to_use`` is a
        # non-empty string here. The runtime ``assert`` documents the
        # contract for ``mypy`` (which can't narrow the kwarg type from
        # the upstream validator) without re-raising.
        assert when_to_use is not None
        group_id = await _resolve_or_create_group(
            session,
            product=product,
            version=version,
            impl_id=impl_id,
            group_key=group_key,
            when_to_use=when_to_use,
            now=now,
        )

    # Natural-key lookup. ``tenant_id IS NULL`` because typed
    # registrations are built-in / global by construction.
    result = await session.execute(
        select(EndpointDescriptor).where(
            EndpointDescriptor.tenant_id.is_(None),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            EndpointDescriptor.op_id == op_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        # Cross-kind re-registration is a programmer error. The natural
        # key ``(tenant_id, product, version, impl_id, op_id)`` does not
        # include ``source_kind``, so without this guard a typed op
        # could be silently re-registered as composite (or vice versa)
        # by an unrelated connector init path -- the row would update
        # everything except ``source_kind``, then at first dispatch
        # the dispatcher would route to the wrong branch and the
        # handler would crash with a :exc:`TypeError` (missing
        # ``dispatch_child`` kwarg, or unexpected one). Fail fast at
        # registration time with the handler's dotted path so the
        # operator can locate the misroute in lifespan logs rather
        # than under request load. Matches the fail-fast posture of
        # :class:`HandlerSignatureError`'s existing cross-rejection
        # checks (typed-handler-in-composite-helper and the inverse).
        if existing.source_kind != source_kind:
            raise HandlerSignatureError(
                "cross-kind re-registration is not supported: "
                f"op (product={product!r}, version={version!r}, "
                f"impl_id={impl_id!r}, op_id={op_id!r}) is already "
                f"registered with source_kind={existing.source_kind!r}; "
                f"refusing to overwrite with source_kind={source_kind!r} "
                f"(handler_ref={handler_ref!r}). If this is intentional, "
                "delete the existing endpoint_descriptor row first."
            )

        existing_text = build_embedding_text(
            summary=existing.summary or "",
            description=existing.description or "",
            custom_description=existing.custom_description,
            tags=existing.tags,
        )
        existing_hash = compute_embedding_text_hash(existing_text)

        if existing_hash == incoming_hash:
            # Skip-re-embed path: embedding text unchanged. UPDATE
            # the non-embedding fields when they differ (cheap), keep
            # the existing embedding intact, advance ``updated_at``.
            # ``summary`` / ``description`` / ``custom_description`` /
            # ``tags`` are deliberately NOT touched here -- their
            # equality is exactly what the hash match proved; writing
            # the same values back would needlessly invalidate any
            # ORM identity-map sharing.
            existing.handler_ref = handler_ref
            existing.parameter_schema = parameter_schema
            existing.response_schema = response_schema
            existing.llm_instructions = llm_instructions
            existing.safety_level = safety_level
            existing.requires_approval = requires_approval
            existing.group_id = group_id
            existing.updated_at = now
            await session.flush()
            if commit:
                await session.commit()
            log.info(
                "operation_registered",
                source_kind=source_kind,
                action="skip_reembed",
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
            )
            return

        # Re-embed path: existing row, embedding text changed.
        embedding = await encode_endpoint_text(
            incoming_text,
            service=embedding_service,
        )
        existing.handler_ref = handler_ref
        existing.summary = summary
        existing.description = description
        existing.custom_description = custom_description
        existing.tags = tags_list
        existing.parameter_schema = parameter_schema
        existing.response_schema = response_schema
        existing.llm_instructions = llm_instructions
        existing.safety_level = safety_level
        existing.requires_approval = requires_approval
        existing.group_id = group_id
        existing.embedding = embedding
        existing.updated_at = now
        await session.flush()
        if commit:
            await session.commit()
        log.info(
            "operation_registered",
            source_kind=source_kind,
            action="reembed",
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=op_id,
        )
        return

    # First-register path: brand-new row.
    embedding = await encode_endpoint_text(
        incoming_text,
        service=embedding_service,
    )
    descriptor = EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
        source_kind=source_kind,
        method=None,
        path=None,
        handler_ref=handler_ref,
        summary=summary,
        description=description,
        group_id=group_id,
        tags=tags_list,
        parameter_schema=parameter_schema,
        response_schema=response_schema,
        llm_instructions=llm_instructions,
        safety_level=safety_level,
        requires_approval=requires_approval,
        is_enabled=True,
        embedding=embedding,
        custom_description=custom_description,
        custom_notes=None,
        created_at=now,
        updated_at=now,
    )
    session.add(descriptor)
    await session.flush()
    if commit:
        await session.commit()
    log.info(
        "operation_registered",
        source_kind=source_kind,
        action="insert",
        product=product,
        version=version,
        impl_id=impl_id,
        op_id=op_id,
    )
