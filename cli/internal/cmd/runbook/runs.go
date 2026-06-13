// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

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

// newRunsCmd returns the `meho runbook runs` command.
//
// CLI shape (per issue #1319):
//
//	meho runbook runs [--assignee <sub>] [--status S]
//	  [--template-slug <slug>] [--limit N] [--json] [--backplane URL]
//
// Wraps GET /api/v1/runbooks/runs. Role: operator (sees own);
// tenant_admin sees all tenant runs unless --assignee narrows.
//
// Role-based scoping is enforced server-side: an OPERATOR's
// `--assignee` filter is ignored by the backend (they only ever see
// their own runs); a TENANT_ADMIN's filter is honoured (or absent =
// all tenant runs). The CLI sends whatever the operator passed and
// renders whatever the backend returns -- no double-checking.
//
// Default output: a 7-column table — RUN_ID (truncated to 8 chars),
// TEMPLATE_SLUG, VERSION, ASSIGNED_TO, STATE, STEP, STARTED_AT.
// `--json` emits the raw RunbookListRunsResponse envelope with full
// UUIDs.
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response
//   - 5   insufficient_role
func newRunsCmd() *cobra.Command {
	var (
		assignee          string
		statusFilter      string
		templateSlug      string
		workRef           string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "runs",
		Short: "List runbook runs in your tenant",
		Long: "runs calls GET /api/v1/runbooks/runs and renders the " +
			"matching runs as a compact 7-column table. Filters: " +
			"--assignee (TENANT_ADMIN: any subject; OPERATOR: ignored " +
			"server-side), --status (in_progress / completed / " +
			"abandoned), --template-slug, --work-ref (exact-match " +
			"change-ticket reference), --limit (1..500, server " +
			"default 100).\n\n" +
			"Operators see only their own runs (the backend forces " +
			"assignee=self regardless of the filter). Tenant_admins see " +
			"all tenant runs unless --assignee narrows. Tenant scoping " +
			"is enforced server-side via the JWT.\n\n" +
			"Output is run-level only -- no step bodies (opacity is " +
			"enforced structurally at the substrate per #1313). To see " +
			"the current step body of an in-progress run you own, use " +
			"`meho runbook next <run_id>`.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runListRuns(cmd, listRunsOptions{
				Assignee:          assignee,
				Status:            statusFilter,
				TemplateSlug:      templateSlug,
				WorkRef:           workRef,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&assignee, "assignee", "",
		"filter by assignee subject (tenant_admin only; operators see own regardless)")
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by run state: in_progress, completed, or abandoned")
	cmd.Flags().StringVar(&templateSlug, "template-slug", "",
		"filter by template slug")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"filter by external change-ticket reference (exact match, e.g. gh:evoila/meho#9)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max runs per page (1..500, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw RunbookListRunsResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listRunsOptions struct {
	Assignee          string
	Status            string
	TemplateSlug      string
	WorkRef           string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runListRuns(cmd *cobra.Command, opts listRunsOptions) error {
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be 0 (use server default) or between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	if opts.Status != "" {
		switch opts.Status {
		case "in_progress", "completed", "abandoned":
			// ok
		default:
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"--status must be one of in_progress, completed, abandoned; got %q",
					opts.Status,
				)),
				opts.JSONOut,
			)
		}
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getRunsList(cmd.Context(), backplaneURL, opts)
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
				"call %s: HTTP 200 without a RunbookListRunsResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printRunsTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listRunsParams maps the CLI flags onto the generated query-param
// shape. Each pointer field is set only when the operator supplied
// the flag so the backplane's own defaults apply for unset values.
//
// Importantly: `--assignee` is pass-through. For OPERATOR callers,
// the backend forces assignee=self regardless of what we send (see
// run_service.list_runs caller_is_admin=False branch); the CLI does
// not pre-filter or double-check. This keeps the role-vs-filter
// resolution in one place server-side -- a future role policy
// change lands without a CLI patch.
func listRunsParams(opts listRunsOptions) *api.ListRunsApiV1RunbooksRunsGetParams {
	params := &api.ListRunsApiV1RunbooksRunsGetParams{}
	if opts.Assignee != "" {
		a := opts.Assignee
		params.Assignee = &a
	}
	if opts.Status != "" {
		s := api.ListRunsApiV1RunbooksRunsGetParamsStatus(opts.Status)
		params.Status = &s
	}
	if opts.TemplateSlug != "" {
		ts := opts.TemplateSlug
		params.TemplateSlug = &ts
	}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		params.WorkRef = &wr
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	return params
}

func getRunsList(
	ctx context.Context,
	backplaneURL string,
	opts listRunsOptions,
) (*api.ListRunsApiV1RunbooksRunsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listRunsParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListRunsApiV1RunbooksRunsGetResponse, error) {
			return authed.ListRunsApiV1RunbooksRunsGetWithResponse(ctx, params)
		},
		func(r *api.ListRunsApiV1RunbooksRunsGetResponse) int { return r.StatusCode() },
	)
}

// printRunsTable renders the list as a 7-column human-readable
// table. The RUN_ID column is truncated to the first 8 chars for
// readability — operators correlating with another surface (`meho
// runbook next`, the audit log) can still match on the prefix in
// most cases; `--json` mode emits the full UUIDs.
//
// The STEP column shows "n/total" for in-progress runs, "-" for
// terminal runs (completed / abandoned have no current step per
// the substrate's RunSummary contract). STARTED_AT is rendered in
// UTC with second resolution -- finer than minute (for run
// correlation across audit log rows) but trimmed of the nanosecond
// tail (which the operator-facing surface doesn't need).
func printRunsTable(w io.Writer, r *api.RunbookListRunsResponse) {
	if r == nil || len(r.Runs) == 0 {
		fmt.Fprintln(w, "no runbook runs in this tenant (matching the filter)")
		return
	}
	fmt.Fprintf(w, "%-9s %-30s %-7s %-20s %-12s %-8s %s\n",
		"RUN_ID", "TEMPLATE_SLUG", "VERSION", "ASSIGNED_TO", "STATE", "STEP", "STARTED_AT")
	for _, run := range r.Runs {
		step := "-"
		if run.Position != nil {
			step = fmt.Sprintf("%d/%d", run.Position.N, run.Position.Total)
		}
		fmt.Fprintf(w, "%-9s %-30s %-7d %-20s %-12s %-8s %s\n",
			truncateRunID(run.RunId.String()),
			truncate(run.TemplateSlug, 30),
			run.TemplateVersion,
			truncate(run.AssignedTo, 20),
			string(run.State),
			step,
			run.StartedAt.UTC().Format("2006-01-02T15:04:05Z"),
		)
	}
}

// truncateRunID returns the first 8 chars of a UUID for the human
// table. The full UUID is 36 chars (8-4-4-4-12 with hyphens); 8 is
// usually enough to disambiguate within a tenant's active runs.
// Operators who need the full id pipe `--json` through jq.
func truncateRunID(id string) string {
	if len(id) <= 8 {
		return id
	}
	return id[:8]
}
