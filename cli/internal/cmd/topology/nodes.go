// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"fmt"
	"io"
)

// Node mirrors the backend TopologyNode Pydantic model
// (backend/src/meho_backplane/topology/schemas.py). `Properties` is a
// free-form JSON object the connector populated; the CLI renders it
// only in --json mode (the table view stays scannable). `ViaEdgeKind`
// is the graph_edge.kind of the edge used to reach this node, or nil
// for the query root (depth 0, reached by no edge). Hand-written for
// the same generated-client-decoupling reason the other verb trees
// document.
type Node struct {
	ID          string         `json:"id"`
	Kind        string         `json:"kind"`
	Name        string         `json:"name"`
	Properties  map[string]any `json:"properties"`
	Depth       int            `json:"depth"`
	ViaEdgeKind *string        `json:"via_edge_kind"`
}

// printNodeClosure renders a dependents/dependencies closure as a
// depth-ordered table. Columns: DEPTH, KIND, NAME, VIA (the edge kind
// the walk traversed to reach the node). The root is depth 0 with an
// empty VIA. Empty closure → the no-result line so an operator can
// distinguish "node exists, no edges" (one row, the root) from "node
// not found in this tenant" (zero rows) — the same distinction the
// backend's one-element-vs-empty contract makes, and the surface
// where a cross-tenant query reads as "not found" rather than leaking
// another tenant's node.
func printNodeClosure(w io.Writer, root string, nodes []Node) {
	if len(nodes) == 0 {
		fmt.Fprintf(w, "no node named %q in this tenant (or no matching closure)\n", root)
		return
	}
	fmt.Fprintf(w, "%-6s %-14s %-40s %s\n", "DEPTH", "KIND", "NAME", "VIA")
	for _, n := range nodes {
		via := "-"
		if n.ViaEdgeKind != nil && *n.ViaEdgeKind != "" {
			via = *n.ViaEdgeKind
		}
		fmt.Fprintf(w, "%-6d %-14s %-40s %s\n",
			n.Depth,
			truncate(n.Kind, 14),
			truncate(n.Name, 40),
			truncate(via, 30),
		)
	}
}
