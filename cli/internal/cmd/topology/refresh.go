// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// RefreshResult mirrors the backend RefreshResult Pydantic model
// (backend/src/meho_backplane/topology/refresh.py). Hand-written
// rather than aliased to a generated client type so the topology
// package stays decoupled from the generated client's surface — the
// targets/operation/kb packages take the same stance for the same
// reason (generated types churn on every spec re-snapshot).
type RefreshResult struct {
	TargetID     string `json:"target_id"`
	AddedNodes   int    `json:"added_nodes"`
	RemovedNodes int    `json:"removed_nodes"`
	UpdatedNodes int    `json:"updated_nodes"`
	AddedEdges   int    `json:"added_edges"`
	RemovedEdges int    `json:"removed_edges"`
	UpdatedEdges int    `json:"updated_edges"`
}

// newRefreshCmd returns the `meho topology refresh <target>` command.
//
//	meho topology refresh <target> [--json] [--backplane <url>]
//	# POST /api/v1/topology/refresh/<target>
//
// Exit codes mirror the sibling verb trees:
//   - 0   refresh completed (any add/remove/update count, including
//     all-zero "no drift")
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (404 no_target with near-misses, etc.)
//   - 5   insufficient_role (403; backend names the required role)
func newRefreshCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "refresh <target>",
		Short: "Rediscover one target's topology and reconcile it into the graph",
		Long: "refresh calls POST /api/v1/topology/refresh/<target>. " +
			"The backend resolves <target> tenant-scoped (alias-aware), " +
			"dispatches to the connector's discover_topology, diffs the " +
			"result against the existing graph, and applies inserts / " +
			"updates / soft-deletes. Renders the per-target node/edge " +
			"add/remove/update counts. A target in another tenant " +
			"resolves to a not-found error (exit 4), identical to a " +
			"typo — cross-tenant refresh is impossible by construction.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRefresh(cmd, refreshOptions{
				Target:            args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type refreshOptions struct {
	Target            string
	JSONOut           bool
	BackplaneOverride string
}

func runRefresh(cmd *cobra.Command, opts refreshOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := postRefresh(cmd.Context(), backplaneURL, opts.Target)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printRefreshSummary(cmd.OutOrStdout(), opts.Target, result)
	return nil
}

// buildRefreshPath assembles the POST path. The target is a single
// path segment; pathEscape keeps an operator-typed name with spaces
// or slashes from corrupting the URL. Exposed for unit tests.
func buildRefreshPath(target string) string {
	return "/api/v1/topology/refresh/" + pathEscape(target)
}

func postRefresh(ctx context.Context, backplaneURL, target string) (*RefreshResult, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST", buildRefreshPath(target), nil)
	if err != nil {
		return nil, err
	}
	var out RefreshResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode refresh response: %w", err)
	}
	return &out, nil
}

// printRefreshSummary renders the reconcile counts as a compact
// two-column summary. The shape mirrors the kb/vault sibling
// convention of a stable key-value block rather than a one-row table.
func printRefreshSummary(w io.Writer, target string, r *RefreshResult) {
	fmt.Fprintf(w, "refreshed topology for %q (target_id=%s)\n", target, r.TargetID)
	fmt.Fprintf(w, "  nodes:  +%d  -%d  ~%d\n", r.AddedNodes, r.RemovedNodes, r.UpdatedNodes)
	fmt.Fprintf(w, "  edges:  +%d  -%d  ~%d\n", r.AddedEdges, r.RemovedEdges, r.UpdatedEdges)
}
