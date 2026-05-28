// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
)

// printNodeClosure renders a dependents/dependencies closure as a
// depth-ordered table. Columns: DEPTH, KIND, NAME, VIA (the edge kind
// the walk traversed to reach the node). The root is depth 0 with an
// empty VIA. Empty closure → the no-result line so an operator can
// distinguish "node exists, no edges" (one row, the root) from "node
// not found in this tenant" (zero rows) — the same distinction the
// backend's one-element-vs-empty contract makes, and the surface
// where a cross-tenant query reads as "not found" rather than leaking
// another tenant's node.
//
// Consumes the generated `api.TopologyNode` type directly per
// G0.12-T15 #1273 — the previously-duplicated local `Node` struct was
// removed in the same change.
func printNodeClosure(w io.Writer, root string, nodes []api.TopologyNode) {
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
