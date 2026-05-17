// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"github.com/spf13/cobra"
)

// newDependentsCmd returns the `meho topology dependents` command.
//
//	meho topology dependents <name|alias> [--depth N] [--kind <edge_kind>]
//	  [--node-kind <node_kind>] [--json] [--backplane <url>]
//	# GET /api/v1/topology/dependents/<name>?depth=N&kind_filter=...&kind=...
//
// Reverse closure: every node that depends on <name> ("what would
// break if I delete this"). The blast-radius verb consumer-needs.md
// L258 specifies — run it *before* recommending a destructive op.
func newDependentsCmd() *cobra.Command {
	var (
		depth             int
		edgeKind          string
		nodeKind          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "dependents <name|alias>",
		Short: "Walk what depends on a node (reverse closure)",
		Long: "dependents calls GET /api/v1/topology/dependents/<name> " +
			"and renders the reverse closure — every node that depends " +
			"on the anchor, depth-ordered. The anchor itself is row 0 " +
			"so an operator can tell \"node exists, no dependents\" " +
			"(one row) from \"node not in this tenant\" (zero rows); a " +
			"cross-tenant name reads as the zero-row case, never " +
			"leaking another tenant's graph. --kind restricts the walk " +
			"to one edge kind; --depth caps the walk (1..64, server " +
			"default 16). If the bare name is ambiguous across node " +
			"kinds the backend returns a 409 naming the kinds; re-run " +
			"with --node-kind <one of them>.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClosure(cmd, closureOptions{
				Verb:              "dependents",
				Name:              args[0],
				Depth:             depth,
				EdgeKind:          edgeKind,
				NodeKind:          nodeKind,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	addClosureFlags(cmd, &depth, &edgeKind, &nodeKind, &backplaneOverride, &jsonOut)
	return cmd
}
