# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology graph traversal surface (Initiative #363, G9.1).

This package is the read side of the per-tenant topology graph. The
schema and ORM models live in :mod:`meho_backplane.db.models`
(``GraphNode`` / ``GraphEdge``, migration ``0007``, Task #448); the
refresh service that populates them is Task #450 (T3). This package
ships Task #451 (T4): the three recursive-CTE query verbs every
blast-radius check and topology question goes through.

Public surface:

* :func:`meho_backplane.topology.query.find_dependents`
* :func:`meho_backplane.topology.query.find_dependencies`
* :func:`meho_backplane.topology.query.find_path`
* :class:`meho_backplane.topology.schemas.TopologyNode`
* :class:`meho_backplane.topology.schemas.TopologyPath`

The API (T5), CLI (T6), and MCP (T7) fronts consume :mod:`query` as a
thin shell and never re-derive the traversal or the tenant boundary.
"""
