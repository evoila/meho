// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, statusCode, body, err := postRefresh(cmd.Context(), backplaneURL, opts.Target)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if result == nil {
		// Guard 200 + missing-content-type leaving JSON200 nil — without
		// this, printRefreshSummary would dereference nil. Mirrors the
		// kb / memory iter-2 nil-guard pattern.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a refresh-result payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printRefreshSummary(cmd.OutOrStdout(), opts.Target, result)
	return nil
}

// buildRefreshPath assembles the POST path. The target is a single
// path segment; pathEscape keeps an operator-typed name with spaces
// or slashes from corrupting the URL. The generated client also uses
// the target name as a path segment internally; this helper stays
// exposed for the unit test that asserts the wire-level path shape.
func buildRefreshPath(target string) string {
	return "/api/v1/topology/refresh/" + pathEscape(target)
}

func postRefresh(
	ctx context.Context,
	backplaneURL, target string,
) (*api.RefreshResult, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RefreshApiV1TopologyRefreshTargetNamePostResponse, error) {
			return authed.RefreshApiV1TopologyRefreshTargetNamePostWithResponse(ctx, target, nil)
		},
		func(r *api.RefreshApiV1TopologyRefreshTargetNamePostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	return resp.JSON200, resp.StatusCode(), resp.Body, nil
}

// printRefreshSummary renders the reconcile counts as a compact
// two-column summary. The shape mirrors the kb/vault sibling
// convention of a stable key-value block rather than a one-row table.
//
// `r.DurationMs` is a float32 in the generated client (the wire
// contract is a Pydantic float); Printf's `%.0f` accepts both.
// `r.TargetId` is a UUID (`openapi_types.UUID` ≡ `uuid.UUID`); its
// `String()` method renders the canonical 8-4-4-4-12 form for the
// audit-correlation line.
func printRefreshSummary(w io.Writer, target string, r *api.RefreshResult) {
	fmt.Fprintf(w, "refreshed topology for %q (target_id=%s)\n", target, r.TargetId)
	fmt.Fprintf(w, "  nodes:  +%d  -%d  ~%d\n", r.AddedNodes, r.RemovedNodes, r.UpdatedNodes)
	fmt.Fprintf(w, "  edges:  +%d  -%d  ~%d\n", r.AddedEdges, r.RemovedEdges, r.UpdatedEdges)
	fmt.Fprintf(w, "  took:   %.0f ms\n", r.DurationMs)
	// #2093 — the backend stamps no_populator_for_product when the
	// target's connector ships no topology populator; without this
	// note the all-zero counts above read as a clean no-op.
	if r.NoPopulatorForProduct != nil {
		fmt.Fprintf(w, "  note:   product %q has no topology populator — the zero counts are a coverage gap, not a clean no-op\n",
			*r.NoPopulatorForProduct)
		if r.PopulatedProducts != nil && len(*r.PopulatedProducts) > 0 {
			fmt.Fprintf(w, "          products with populators: %s\n",
				strings.Join(*r.PopulatedProducts, ", "))
		}
	}
}
