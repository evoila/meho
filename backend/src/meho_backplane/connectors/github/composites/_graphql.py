# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared GraphQL transport for the gh-rest board composites (#2081).

GitHub's Projects-v2 surface is **GraphQL-only** -- a REST connector
structurally cannot add a project item or set a single-select field
(the issue-body framing). The board composites in :mod:`._board`
therefore POST GraphQL documents to ``/graphql`` through the resolved
:class:`~meho_backplane.connectors.github.connector.GitHubRestConnector`'s
own authenticated session (the #2255 direct-session substrate the read
composite already uses), so they work on a fresh deploy with **zero gh
catalog ingest**.

Why a dedicated helper
----------------------

GitHub's GraphQL endpoint returns **HTTP 200 even when the query
fails** -- a bad node id, a permissions error, or a malformed mutation
comes back as a ``200`` whose JSON body carries a non-empty ``errors``
array and (usually) ``data: null``. :meth:`HttpConnector._post_json`
only raises on the HTTP status (via ``raise_for_status``), so it would
hand a caller an ``errors``-bearing envelope as if it were success.
:func:`github_graphql` closes that gap: it inspects the parsed body,
raises :class:`GitHubGraphQlError` when ``errors`` is present, and
returns the unwrapped ``data`` object otherwise. Every board composite
routes through it so the failure mode is uniform (a write never
silently no-ops on a GraphQL error).

Variables, not string interpolation
------------------------------------

Every dynamic value (login, project number, node ids, option id) is
passed through the GraphQL ``variables`` map, never spliced into the
query text. This is the GraphQL analogue of a parameterised SQL
statement -- it is injection-safe and lets GitHub type-check the
document against its schema. The only value composed into query text is
the root selector (``organization`` vs ``user``), which the board
handler picks from a closed literal map keyed off a schema-validated
enum, never from raw caller input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = ["GITHUB_GRAPHQL_PATH", "GitHubGraphQlError", "github_graphql"]

#: Wire path for GitHub's GraphQL endpoint. The connector mounts it
#: under ``https://api.github.com`` (``_base_url``), so the full URL is
#: ``https://api.github.com/graphql``. GHES would remap this (``/api/
#: graphql``), but T1 ships github.com only -- consistent with the rest
#: of the connector's github.com-only scope.
GITHUB_GRAPHQL_PATH = "/graphql"


class GitHubGraphQlError(RuntimeError):
    """A GitHub GraphQL call returned a non-empty ``errors`` array (or no data).

    GitHub answers a failed GraphQL operation with ``HTTP 200`` and an
    ``errors`` array in the body rather than a 4xx/5xx status, so this
    is raised by :func:`github_graphql` after inspecting the parsed
    payload -- not by the transport layer's ``raise_for_status``.

    Subclasses :class:`RuntimeError` so the dispatcher's outer exception
    branch wraps it as a structured ``connector_error``
    :class:`~meho_backplane.connectors.schemas.OperationResult`, the
    same way the read composite's malformed-payload ``RuntimeError`` is
    surfaced. The GraphQL error messages are folded into the exception
    string so the operator sees *why* the board op failed (unknown node
    id, insufficient project scope, etc.).
    """

    def __init__(self, message: str, *, errors: list[Any] | None = None) -> None:
        self.errors = errors or []
        super().__init__(message)


def _summarise_graphql_errors(errors: list[Any]) -> str:
    """Join the ``message`` field of each GraphQL error into one string.

    GitHub GraphQL errors are objects carrying a ``message`` (and often
    a ``type`` / ``path``); the message is the operator-actionable part.
    Non-dict / message-less entries are stringified verbatim so a
    surprising error shape still surfaces rather than disappearing.
    """
    messages: list[str] = []
    for err in errors:
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            messages.append(err["message"])
        else:
            messages.append(repr(err))
    return "; ".join(messages) if messages else "<no error messages>"


async def github_graphql(
    connector: GitHubRestConnector,
    target: Any,
    operator: Operator,
    *,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """POST a GraphQL document to ``/graphql`` and return its ``data`` object.

    Issues the call through *connector*'s own session
    (:meth:`~meho_backplane.connectors.adapters.http.HttpConnector._post_json`,
    which carries the connector's ``Authorization: Bearer`` header) with
    the standard ``{"query": ..., "variables": ...}`` request body.

    Returns
    -------
    dict[str, Any]
        The unwrapped ``data`` object (the top-level ``data`` key of the
        GraphQL response), guaranteed non-``None`` on return.

    Raises
    ------
    GitHubGraphQlError
        The response body was not a JSON object, carried a non-empty
        ``errors`` array, or had a ``null`` / missing ``data`` object.
        GitHub returns these as ``HTTP 200``, so they cannot surface via
        the transport's ``raise_for_status``.
    httpx.HTTPError
        A transport-level or non-2xx failure (auth 401, rate-limit 403,
        5xx). Propagates from ``_post_json`` for the dispatcher's outer
        branch to map to the matching structured result.
    """
    payload = await connector._post_json(
        target,
        GITHUB_GRAPHQL_PATH,
        operator=operator,
        json={"query": query, "variables": variables},
    )
    if not isinstance(payload, dict):
        raise GitHubGraphQlError(
            f"GitHub GraphQL response was not a JSON object (got {type(payload).__name__})"
        )
    errors = payload.get("errors")
    if errors:
        error_list = errors if isinstance(errors, list) else [errors]
        raise GitHubGraphQlError(
            f"GitHub GraphQL call failed: {_summarise_graphql_errors(error_list)}",
            errors=error_list,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GitHubGraphQlError(
            "GitHub GraphQL response carried no ``data`` object "
            f"(got {type(data).__name__}); the query returned neither data nor errors"
        )
    return data
