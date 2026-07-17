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
// the walk traversed to reach the node), PARENT (the node this row
// hangs off — #2538 chain provenance). The root is depth 0 with an
// empty VIA and PARENT.
//
// PARENT renders the parent node's name resolved from the closure
// itself (every parent_node_id points at another row of the same
// result); if a payload ever carries a parent id missing from the
// result set (a contract drift), the raw UUID is printed so the
// information is never silently dropped. `--json` carries the full
// `parent_node_id` / `via_edge_id` fields untouched.
//
// G0.18-T4 (#1357) changed the not-found contract: a closure on an
// untracked anchor returns HTTP 404 `node_untracked` and is
// rendered by `formatNotFound` upstream — it never reaches this
// function. The minimum payload here is the one-element `[root]`
// for a tracked-but-no-dependents node. The defensive zero-row
// branch stays as a structural guard against a future contract
// drift (or an unforeseen empty response from a service patched at
// the test seam) and still produces an operator-readable line, but
// in normal operation it is unreachable on the dependents /
// dependencies routes.
//
// Consumes the generated `api.TopologyNode` type directly per
// G0.12-T15 #1273 — the previously-duplicated local `Node` struct was
// removed in the same change.
func printNodeClosure(w io.Writer, root string, nodes []api.TopologyNode) {
	if len(nodes) == 0 {
		fmt.Fprintf(w, "no closure rows returned for %q (unexpected — closure routes return 404 node_untracked rather than an empty 200 since G0.18-T4 #1357)\n", root)
		return
	}
	nameByID := make(map[string]string, len(nodes))
	for _, n := range nodes {
		nameByID[n.Id.String()] = n.Name
	}
	fmt.Fprintf(w, "%-6s %-14s %-40s %-20s %s\n", "DEPTH", "KIND", "NAME", "VIA", "PARENT")
	for _, n := range nodes {
		via := "-"
		if n.ViaEdgeKind != nil && *n.ViaEdgeKind != "" {
			via = *n.ViaEdgeKind
		}
		parent := "-"
		if n.ParentNodeId != nil {
			parent = n.ParentNodeId.String()
			if name, ok := nameByID[parent]; ok {
				parent = name
			}
		}
		fmt.Fprintf(w, "%-6d %-14s %-40s %-20s %s\n",
			n.Depth,
			truncate(n.Kind, 14),
			truncate(n.Name, 40),
			truncate(via, 20),
			truncate(parent, 40),
		)
	}
}
