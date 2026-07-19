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
//	meho agent run-list [--work-ref REF] [--status S] [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Lists the tenant's agent runs via GET /api/v1/agents/runs,
// newest first. Filters: --work-ref (exact-match external change-ticket
// reference, work_ref I3-T2 #1662), --status, and --agent-name (exact-match
// agent definition name, #2472). Tenant-isolated server-side via the JWT —
// only the caller's tenant's runs are visible.
func newRunListCmd() *cobra.Command {
	var (
		workRef           string
		statusFilter      string
		agentName         string
		limit             int
		offset            int
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
			"--agent-name (exact-match agent definition name — an unknown " +
			"name returns an empty list), --limit (1..500, server default " +
			"100), --offset (rows to skip for paging, default 0). Runs are " +
			"tenant-isolated server-side — only your tenant's runs " +
			"appear. --json emits the raw list for scripting.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRunList(cmd, runListOptions{
				WorkRef:           workRef,
				Status:            statusFilter,
				AgentName:         agentName,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"filter by external change-ticket reference (exact match, e.g. gh:evoila/meho#11)")
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by lifecycle status: pending, running, awaiting_approval, succeeded, failed, or cancelled")
	cmd.Flags().StringVar(&agentName, "agent-name", "",
		"filter by agent definition name (exact match); an unknown name returns an empty list")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max runs per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"rows to skip for paging into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw []AgentRunSummaryResponse JSON instead of the table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runListOptions struct {
	WorkRef           string
	Status            string
	AgentName         string
	Limit             int
	Offset            int
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
	if opts.AgentName != "" {
		an := opts.AgentName
		params.AgentName = &an
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Offset > 0 {
		o := opts.Offset
		params.Offset = &o
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
	fmt.Fprintf(w, "%-36s  %-18s  %-20s  %-12s  %-24s  %s\n",
		"RUN ID", "STATUS", "AGENT", "TRIGGER", "CREATED", "WORK_REF")
	for _, r := range *runs {
		workRef := "-"
		if r.WorkRef != nil && *r.WorkRef != "" {
			workRef = *r.WorkRef
		}
		agentName := "-"
		if r.AgentName != nil && *r.AgentName != "" {
			agentName = *r.AgentName
		}
		fmt.Fprintf(w, "%-36s  %-18s  %-20s  %-12s  %-24s  %s\n",
			r.RunId.String(),
			string(r.Status),
			agentName,
			r.Trigger,
			r.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"),
			workRef,
		)
	}
}
