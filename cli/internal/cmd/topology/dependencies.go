// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"github.com/spf13/cobra"
)

// newDependenciesCmd returns the `meho topology dependencies` command.
//
//	meho topology dependencies <name|alias> [--depth N] [--kind <edge_kind>]
//	  [--node-kind <node_kind>] [--include-stale=false] [--json] [--backplane <url>]
//	# GET /api/v1/topology/dependencies/<name>?depth=N&kind_filter=...&kind=...&include_stale=...
//
// Forward closure: everything <name> depends on ("what I need to be
// healthy"). The mirror of `dependents` — same shape, same one-row-
// per-node dedupe, same tenant scoping — walking edges out of the
// node rather than into it.
func newDependenciesCmd() *cobra.Command {
	var (
		depth             int
		edgeKind          string
		nodeKind          string
		includeStale      bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "dependencies <name|alias>",
		Short: "Walk what a node depends on (forward closure)",
		Long: "dependencies calls GET /api/v1/topology/dependencies/" +
			"<name> and renders the forward closure — everything the " +
			"anchor depends on, depth-ordered. Same one-row-per-node, " +
			"same tenant scoping, same --kind / --depth / --node-kind " +
			"contract as `dependents`; only the walk direction differs " +
			"(out of the node instead of into it).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runClosure(cmd, closureOptions{
				Verb:              "dependencies",
				Name:              args[0],
				Depth:             depth,
				EdgeKind:          edgeKind,
				NodeKind:          nodeKind,
				IncludeStale:      includeStale,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	addClosureFlags(cmd, &depth, &edgeKind, &nodeKind, &backplaneOverride, &includeStale, &jsonOut)
	return cmd
}
