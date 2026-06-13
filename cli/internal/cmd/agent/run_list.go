// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRunListCmd returns the `meho agent run-list` command.
//
//	meho agent run-list [--work-ref REF] [--status S] [--limit N] [--json] [--backplane <url>]
//
// Role: operator. Lists the tenant's agent runs via GET /api/v1/agents/runs,
// newest first. Filters: --work-ref (exact-match external change-ticket
// reference, work_ref I3-T2 #1662) and --status. Tenant-isolated
// server-side via the JWT — only the caller's tenant's runs are visible.
func newRunListCmd() *cobra.Command {
	var (
		workRef           string
		statusFilter      string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run-list",
		Short: "List agent runs (filter by --work-ref / --status)",
		Long: "run-list calls GET /api/v1/agents/runs and renders the " +
			"tenant's runs as a compact table, newest first. Filters: " +
			"--work-ref (exact-match change-ticket reference, e.g. " +
			"gh:evoila/meho#11), --status (pending / running / " +
			"awaiting_approval / succeeded / failed / cancelled), " +
			"--limit (1..500, server default 100). Runs are " +
			"tenant-isolated server-side — only your tenant's runs " +
			"appear. --json emits the raw list for scripting.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRunList(cmd, runListOptions{
				WorkRef:           workRef,
				Status:            statusFilter,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"filter by external change-ticket reference (exact match, e.g. gh:evoila/meho#11)")
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by lifecycle status: pending, running, awaiting_approval, succeeded, failed, or cancelled")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max runs per page (1..500, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw []AgentRunSummaryResponse JSON instead of the table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runListOptions struct {
	WorkRef           string
	Status            string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runRunList(cmd *cobra.Command, opts runListOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := listRuns(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printRunList(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listRunsParams maps the CLI options onto the generated query-param struct.
// Empty filters are left nil so the server applies its defaults (no filter,
// server-side page size).
func listRunsParams(opts runListOptions) *api.ListRunsApiV1AgentsRunsGetParams {
	params := &api.ListRunsApiV1AgentsRunsGetParams{}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		params.WorkRef = &wr
	}
	if opts.Status != "" {
		s := api.AgentRunStatus(opts.Status)
		params.Status = &s
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	return params
}

func listRuns(ctx context.Context, backplaneURL string, opts runListOptions) (*api.ListRunsApiV1AgentsRunsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listRunsParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListRunsApiV1AgentsRunsGetResponse, error) {
			return authed.ListRunsApiV1AgentsRunsGetWithResponse(ctx, params)
		},
		func(r *api.ListRunsApiV1AgentsRunsGetResponse) int { return r.StatusCode() },
	)
}

// printRunList renders the run list as a compact table. An empty list
// prints a single "no runs" line so a scripted caller sees a clear signal.
func printRunList(w io.Writer, runs *[]api.AgentRunSummaryResponse) {
	if runs == nil || len(*runs) == 0 {
		fmt.Fprintln(w, "no agent runs")
		return
	}
	fmt.Fprintf(w, "%-36s  %-18s  %-12s  %-24s  %s\n",
		"RUN ID", "STATUS", "TRIGGER", "CREATED", "WORK_REF")
	for _, r := range *runs {
		workRef := "-"
		if r.WorkRef != nil && *r.WorkRef != "" {
			workRef = *r.WorkRef
		}
		fmt.Fprintf(w, "%-36s  %-18s  %-12s  %-24s  %s\n",
			r.RunId.String(),
			string(r.Status),
			r.Trigger,
			r.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"),
			workRef,
		)
	}
}
