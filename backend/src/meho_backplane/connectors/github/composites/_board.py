# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Projects-v2 board ``gh.composite.*`` handlers (3 composites, #2081).

The gh-rest connector can create an issue (the one live REST op) but a
board-driven team's ticket is only complete once it lands on the
Projects-v2 board with its Status / Priority / Size fields set. That
surface is **GraphQL-only** -- ``addProjectV2ItemById`` /
``updateProjectV2ItemFieldValue`` have no REST equivalent -- so before
this Task a board-complete filing dropped back to the ``gh`` CLI. These
three composites close that gap through the connector's own dispatch
path (auth + policy + audit + JSONFlux + broadcast):

* :func:`project_view_composite` (``gh.composite.project_view``) --
  read the board in one call: its node id, its fields (with the option
  ids of every single-select field), and its current items. This is the
  ergonomic linchpin -- it hands back the ``project_id`` / ``field_id``
  / ``option_id`` node ids the two write composites below consume, so an
  agent never has to shell out to ``gh project`` to discover them.
* :func:`project_item_add_composite` (``gh.composite.project_item_add``)
  -- add an issue / PR (by content node id) to a board.
* :func:`project_item_set_field_composite`
  (``gh.composite.project_item_set_field``) -- set a single-select field
  (Status / Priority / Size) on a board item.

Direct-session dispatch (#2255)
-------------------------------

Each handler declares a ``connector`` parameter and issues its GraphQL
call through the resolved
:class:`~meho_backplane.connectors.github.connector.GitHubRestConnector`'s
own session via :func:`._graphql.github_graphql`, with no
``endpoint_descriptor`` lookup -- the same substrate the read composite
uses, so the board ops work on a fresh deploy with zero gh catalog
ingest.

Governance posture
------------------

``project_view`` is a read (``safety_level="safe"`` /
``requires_approval=False``). The two write composites are
``safety_level="caution"`` / ``requires_approval=False`` (set in
:mod:`._register`): adding a card to a board and setting a status field
are low-blast-radius, reversible board-hygiene mutations -- honestly a
write, but not ``dangerous`` like the vmware VM-delete / host-maintenance
composites. Each is a *single* logical GraphQL mutation, so the
composite itself is the reviewed unit: the dispatcher runs
``policy_gate`` against the composite's own descriptor before invoking
the handler, which fully governs the one write it performs. There is no
heterogeneous internal write sub-op to protect, so -- unlike the
multi-write vmware composites -- these do not need the
``enforce_subop_policy`` seam (which exists to re-gate *additional*
writes a direct-session composite fans out to). Under this posture a
human / service operator auto-executes a board write (board-complete
filing without a ``gh`` CLI fallback), while an agent principal routes
to the approval queue via the ``caution`` safety default -- exactly the
governance a raw CLI fallback would have bypassed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.github.composites._graphql import (
    GitHubGraphQlError,
    github_graphql,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.github.connector import GitHubRestConnector

__all__ = [
    "project_item_add_composite",
    "project_item_set_field_composite",
    "project_view_composite",
]

# Root selector for a Projects-v2 lookup keyed by ``owner_type``. Both
# values are hardcoded GraphQL field names, never caller input -- the
# parameter schema pins ``owner_type`` to this closed enum, and the
# handler maps it here before composing the query text. Every *dynamic*
# value (login, number, node ids) rides the GraphQL ``variables`` map.
_OWNER_TYPE_ROOT: dict[str, str] = {"organization": "organization", "user": "user"}

# ``__ROOT__`` is replaced with the closed-map literal above (org / user);
# ``$login`` / ``$number`` / ``$first`` are GraphQL variables. Reads the
# project node id + every field (single-select fields additionally carry
# their ``options {id name}``) + the current items, each with its own
# Status / Priority / Size / ... field values, in one round-trip.
#
# ``fieldValues(first: 20)`` is a per-item bound, not a page size: an item
# carries one value per set field, and auto-populated values (assignees,
# labels, linked PRs, repository) precede the custom single-selects in the
# connection. The live MEHO board's busiest items hold 9 values, where
# ``first: 8`` drops the trailing ``Track`` single-select on every assigned
# item; 20 clears that observed max with headroom and needs no per-item
# cursor (board-item field-value pagination is a separate follow-on).
_PROJECT_VIEW_QUERY = """
query ($login: String!, $number: Int!, $first: Int!) {
  __ROOT__(login: $login) {
    projectV2(number: $number) {
      id
      title
      fields(first: 50) {
        nodes {
          __typename
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { options { id name } }
        }
      }
      items(first: $first) {
        totalCount
        nodes {
          id
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue { number title url }
            ... on PullRequest { number title url }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
""".strip()

_ADD_ITEM_MUTATION = """
mutation ($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
""".strip()

_SET_FIELD_MUTATION = """
mutation ($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: { singleSelectOptionId: $optionId }
    }
  ) {
    projectV2Item { id }
  }
}
""".strip()


def _extract_project(data: dict[str, Any], owner_type: str) -> dict[str, Any]:
    """Return the ``projectV2`` object from a project-view response, or raise.

    A ``null`` owner (login typo / no access) or ``null`` project
    (number does not exist / not visible to the token) both surface as a
    :class:`GitHubGraphQlError` rather than a chained-lookup crash, so
    the operator learns which of the two failed.
    """
    root = _OWNER_TYPE_ROOT[owner_type]
    owner = data.get(root)
    if not isinstance(owner, dict):
        raise GitHubGraphQlError(
            f"GitHub GraphQL returned no {root!r} for the requested login "
            f"(unknown login, or the token cannot see it)"
        )
    project = owner.get("projectV2")
    if not isinstance(project, dict):
        raise GitHubGraphQlError(
            "GitHub GraphQL returned no projectV2 for the requested number "
            "(no board with that number, or the token lacks project scope)"
        )
    return project


def _parse_fields(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse the ``fields.nodes`` array into ``{id, name, data_type, options}``.

    ``options`` is a list of ``{id, name}`` for single-select fields
    (the ids an operator threads into ``project_item_set_field``) and
    ``None`` for every other field type. Rows missing an id / name (an
    unexpected upstream shape) are dropped defensively.
    """
    fields_conn = project.get("fields")
    nodes = fields_conn.get("nodes") if isinstance(fields_conn, dict) else None
    if not isinstance(nodes, list):
        return []
    parsed: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        field_id = node.get("id")
        name = node.get("name")
        if not isinstance(field_id, str) or not isinstance(name, str):
            continue
        raw_options = node.get("options")
        options: list[dict[str, Any]] | None = None
        if isinstance(raw_options, list):
            options = [
                {"id": opt.get("id"), "name": opt.get("name")}
                for opt in raw_options
                if isinstance(opt, dict)
            ]
        parsed.append(
            {"id": field_id, "name": name, "data_type": node.get("dataType"), "options": options}
        )
    return parsed


def _parse_field_values(node: dict[str, Any]) -> dict[str, Any]:
    """Collapse an item's ``fieldValues.nodes`` array into ``{field name: value}``.

    Only single-select (its selected option ``name``) and plain-text (its
    ``text``) value types are projected -- the Status / Priority / Size
    fields the board-status read needs, plus the common free-text field.
    Every other value type (iteration, number, date, repository, labels,
    ...) is dropped, mirroring :func:`_parse_fields`' "skip unexpected
    shapes" defensiveness; a node whose owning field name cannot be
    resolved is likewise skipped rather than raised.
    """
    values_conn = node.get("fieldValues")
    nodes = values_conn.get("nodes") if isinstance(values_conn, dict) else None
    if not isinstance(nodes, list):
        return {}
    parsed: dict[str, Any] = {}
    for value_node in nodes:
        if not isinstance(value_node, dict):
            continue
        typename = value_node.get("__typename")
        if typename == "ProjectV2ItemFieldSingleSelectValue":
            value = value_node.get("name")
        elif typename == "ProjectV2ItemFieldTextValue":
            value = value_node.get("text")
        else:
            continue
        field = value_node.get("field")
        field_name = field.get("name") if isinstance(field, dict) else None
        if not isinstance(field_name, str):
            continue
        parsed[field_name] = value
    return parsed


def _parse_items(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Collapse the ``items.nodes`` array into ``{item_id, content_type, ...}`` rows.

    Each row carries the board ``item_id`` (the node id
    ``project_item_set_field`` needs) plus the underlying content's type,
    number, title, and url, and a ``field_values`` map of the item's set
    Status / Priority / Size / text fields (see :func:`_parse_field_values`).
    Draft issues have no number / url; those keys surface as ``None``. An
    item with no projected field values carries an empty ``field_values``.
    """
    items_conn = project.get("items")
    nodes = items_conn.get("nodes") if isinstance(items_conn, dict) else None
    if not isinstance(nodes, list):
        return []
    parsed: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        content = node.get("content")
        content_dict = content if isinstance(content, dict) else {}
        parsed.append(
            {
                "item_id": node.get("id"),
                "content_type": content_dict.get("__typename"),
                "number": content_dict.get("number"),
                "title": content_dict.get("title"),
                "url": content_dict.get("url"),
                "field_values": _parse_field_values(node),
            }
        )
    return parsed


def _item_count(project: dict[str, Any]) -> int | None:
    """Return the board's ``items.totalCount`` (all items, not just the page)."""
    items_conn = project.get("items")
    if not isinstance(items_conn, dict):
        return None
    total = items_conn.get("totalCount")
    return total if isinstance(total, int) else None


async def project_view_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Read a Projects-v2 board: node id + fields (with options) + items.

    Op-id: ``gh.composite.project_view``. One GraphQL round-trip returns
    the ``project_id`` (needed to add items / set fields), every field
    with the option ids of single-select fields, and the current items.

    Raises
    ------
    GitHubGraphQlError
        The login / project number did not resolve, or the GraphQL call
        itself returned an ``errors`` array.
    """
    owner = params["owner"]
    project_number = params["project_number"]
    owner_type = params.get("owner_type", "organization")
    if owner_type not in _OWNER_TYPE_ROOT:
        raise GitHubGraphQlError(
            f"owner_type must be one of {sorted(_OWNER_TYPE_ROOT)}; got {owner_type!r}"
        )
    first = int(params.get("first", 20))

    query = _PROJECT_VIEW_QUERY.replace("__ROOT__", _OWNER_TYPE_ROOT[owner_type])
    data = await github_graphql(
        connector,
        target,
        operator,
        query=query,
        variables={"login": owner, "number": project_number, "first": first},
    )
    project = _extract_project(data, owner_type)
    return {
        "project_id": project.get("id"),
        "title": project.get("title"),
        "fields": _parse_fields(project),
        "items": _parse_items(project),
        "item_count": _item_count(project),
    }


async def project_item_add_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Add an issue / PR to a Projects-v2 board by content node id.

    Op-id: ``gh.composite.project_item_add``. Wraps the
    ``addProjectV2ItemById`` GraphQL mutation. ``project_id`` and
    ``content_id`` are GraphQL node ids (the ``project_id`` from
    :func:`project_view_composite`; the ``content_id`` is the issue / PR
    ``node_id`` the REST create-issue response carries). Idempotent on
    GitHub's side -- re-adding an already-present item returns the
    existing item's id.

    Raises
    ------
    GitHubGraphQlError
        The mutation returned an ``errors`` array (unknown node id,
        insufficient project scope) or a payload without an item id.
    """
    project_id = params["project_id"]
    content_id = params["content_id"]
    data = await github_graphql(
        connector,
        target,
        operator,
        query=_ADD_ITEM_MUTATION,
        variables={"projectId": project_id, "contentId": content_id},
    )
    item_id = _extract_node_id(data, mutation="addProjectV2ItemById", node_key="item")
    return {
        "item_id": item_id,
        "project_id": project_id,
        "content_id": content_id,
        "status": "added",
    }


async def project_item_set_field_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: GitHubRestConnector,
) -> dict[str, Any]:
    """Set a single-select field (Status / Priority / Size) on a board item.

    Op-id: ``gh.composite.project_item_set_field``. Wraps the
    ``updateProjectV2ItemFieldValue`` GraphQL mutation with a
    ``singleSelectOptionId`` value. Every argument is a GraphQL node id
    resolvable from :func:`project_view_composite` (``project_id`` +
    ``item_id`` + each field's ``id`` + the chosen option's ``id``).

    Raises
    ------
    GitHubGraphQlError
        The mutation returned an ``errors`` array (unknown id, or the
        option does not belong to the field) or a payload without an
        item id.
    """
    project_id = params["project_id"]
    item_id = params["item_id"]
    field_id = params["field_id"]
    option_id = params["option_id"]
    data = await github_graphql(
        connector,
        target,
        operator,
        query=_SET_FIELD_MUTATION,
        variables={
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "optionId": option_id,
        },
    )
    updated_item_id = _extract_node_id(
        data, mutation="updateProjectV2ItemFieldValue", node_key="projectV2Item"
    )
    return {
        "item_id": updated_item_id,
        "field_id": field_id,
        "option_id": option_id,
        "status": "updated",
    }


def _extract_node_id(data: dict[str, Any], *, mutation: str, node_key: str) -> str:
    """Pull ``data[mutation][node_key]["id"]`` out of a mutation response, or raise.

    Both board mutations return a single nested object carrying the
    affected item's node id (``addProjectV2ItemById -> item -> id``,
    ``updateProjectV2ItemFieldValue -> projectV2Item -> id``). A missing
    id means the mutation reported success shape but no node -- surfaced
    as :class:`GitHubGraphQlError` rather than returning ``None``.
    """
    outer = data.get(mutation)
    node = outer.get(node_key) if isinstance(outer, dict) else None
    node_id = node.get("id") if isinstance(node, dict) else None
    if not isinstance(node_id, str) or not node_id:
        raise GitHubGraphQlError(
            f"GitHub GraphQL {mutation} returned no {node_key}.id (payload shape: {data!r})"
        )
    return node_id
