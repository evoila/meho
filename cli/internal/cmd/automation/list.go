// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package automation

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

// newListCmd returns the `meho automation list` command.
//
// Task #3029. The paired-surface discovery verb: it lists the add-on(s)
// advertising the `automation` meta-tool family and the surface each declares,
// mirroring the `meho_automation_list` MCP tool and `GET /api/v1/automation`.
// A read every operator may run; there is no client-side pairing gate (#2109) —
// the backplane returns 403 `automation_addon_not_active` while nothing is
// paired, which the CLI renders as a clear "not active" message.
//
// CLI shape:
//
//	meho automation list \
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes mirror the sibling docs verbs:
//   - 0   surface listed cleanly
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. the inactive-surface 403)
//   - 5   insufficient_role
func newListCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the paired automation add-on surface",
		Long: "list calls GET /api/v1/automation and renders the automation " +
			"add-on(s) currently paired and contract-healthy, each with its " +
			"negotiated contract version, whether that version is still " +
			"compatible with this backplane, its last liveness heartbeat, and " +
			"the surfaces it advertises (meta-tool / CLI verb families, console " +
			"panels, event kinds). When no automation add-on is paired the " +
			"surface is inactive and the command reports that (exit 4). --json " +
			"emits the raw API response so operators can pipe into jq.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// listOptions is the flag set for the automation list verb.
type listOptions struct {
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := listSurface(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without an automation-surface payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), *resp.JSON200)
	}
	printSurfaceTable(cmd.OutOrStdout(), *resp.JSON200)
	return nil
}

func listSurface(
	ctx context.Context,
	backplaneURL string,
) (*api.ListAutomationSurfaceApiV1AutomationGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	// No query parameters — the surface is tenant-scoped from the bearer; the
	// generated Authorization param is injected by the authed client's editor.
	params := &api.ListAutomationSurfaceApiV1AutomationGetParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListAutomationSurfaceApiV1AutomationGetResponse, error) {
			return authed.ListAutomationSurfaceApiV1AutomationGetWithResponse(ctx, params)
		},
		func(r *api.ListAutomationSurfaceApiV1AutomationGetResponse) int {
			return r.StatusCode()
		},
	)
}

// printSurfaceTable renders the automation surface as a compact table. Columns:
// ADD-ON, CONTRACT, COMPATIBLE, SURFACES — the fields an operator reads to see
// whether governed automation is live and what it exposes. Timestamps are
// omitted from the human view; --json surfaces the full response.
func printSurfaceTable(w io.Writer, surface api.AutomationSurfaceResponse) {
	if len(surface.Providers) == 0 {
		fmt.Fprintln(w, "no automation add-on is paired and contract-healthy")
		return
	}
	fmt.Fprintf(w, "%-20s %-10s %-11s %s\n", "ADD-ON", "CONTRACT", "COMPATIBLE", "SURFACES")
	for _, p := range surface.Providers {
		fmt.Fprintf(w, "%-20s %-10s %-11s %s\n",
			truncate(p.Addon, 20),
			fmt.Sprintf("v%d", p.ContractVersion),
			formatBool(p.ContractCompatible),
			truncate(formatSurfaces(p.Surfaces), 60),
		)
	}
}

// formatSurfaces renders a provider's advertised surfaces as a comma-separated
// `kind:name` list, "-" when it advertises none.
func formatSurfaces(surfaces []api.AutomationSurfaceEntry) string {
	if len(surfaces) == 0 {
		return "-"
	}
	parts := make([]string, 0, len(surfaces))
	for _, s := range surfaces {
		parts = append(parts, fmt.Sprintf("%s:%s", string(s.Kind), s.Name))
	}
	return strings.Join(parts, ",")
}

// formatBool renders a compatibility flag as yes/no for the human table.
func formatBool(v bool) string {
	if v {
		return "yes"
	}
	return "no"
}
