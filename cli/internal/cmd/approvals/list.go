// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho approvals list` command.
//
//	meho approvals list [--status pending|approved|rejected|expired|all] [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Lists approval requests for the operator's tenant,
// newest-first, via GET /api/v1/approvals.
func newListCmd() *cobra.Command {
	var (
		statusFilter      string
		workRef           string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List approval requests in your tenant",
		Long: "list calls GET /api/v1/approvals and renders approval " +
			"requests in the operator's tenant, newest first. By " +
			"default it shows only pending requests: the ones that " +
			"wait for a decision. Use --status all to see requests in " +
			"every state, or --status approved|rejected|expired for " +
			"one state. --work-ref filters to the requests " +
			"authorised by an external change ticket (exact match, " +
			"e.g. gh:evoila/meho#1). --limit caps how many requests " +
			"come back (1..500; no limit when omitted). --offset " +
			"skips that many requests first. --json emits the raw " +
			"JSON array for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOpts{
				StatusFilter:      statusFilter,
				WorkRef:           workRef,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by status: pending, approved, rejected, expired, all (default: pending)")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"filter by external change-ticket reference, exact match (e.g. gh:evoila/meho#1)")
	// 0 means "not set": the flag is then left off the request and the
	// server applies no limit / no offset.
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max requests to return (1..500; no limit when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"skip this many requests first (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw JSON array instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

type listOpts struct {
	StatusFilter      string
	WorkRef           string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOpts) error {
	validStatuses := map[string]bool{
		"pending": true, "approved": true, "rejected": true, "expired": true, "all": true,
	}
	if opts.StatusFilter != "" && !validStatuses[opts.StatusFilter] {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--status must be one of: pending, approved, rejected, expired, all; got %q",
				opts.StatusFilter,
			)),
			opts.JSONOut,
		)
	}
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	if opts.Offset < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be non-negative; got %d", opts.Offset)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	items, err := fetchList(cmd.Context(), client, opts)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if items == nil {
		// Defensive: a 2xx with a missing JSON200 means the server's
		// response didn't decode against ApprovalRequestView (schema
		// drift). The OpenAPI freshness gate catches new fields; a
		// `nil` JSON200 here would mean the FastAPI side returned
		// 200 with a body that doesn't match the spec — unexpected.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView"),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), items)
	}
	printListTable(cmd.OutOrStdout(), items, opts.StatusFilter, opts.Offset)
	return nil
}

// fetchList drives the typed-client `ListApprovalsApiV1ApprovalsGet`
// endpoint with a one-shot 401-retry around the underlying
// AuthedClient's refresh path (mirrors `AuthedClient.GetHealth`'s
// pattern in client.go). Non-2xx responses are returned as
// `*httpResponseError` so the caller can route them through
// `renderHTTPStatus`; transport-layer errors return verbatim.
func fetchList(
	ctx context.Context,
	client *api.AuthedClient,
	opts listOpts,
) ([]api.ApprovalRequestView, error) {
	params := &api.ListApprovalsApiV1ApprovalsGetParams{}
	if opts.StatusFilter != "" {
		s := opts.StatusFilter
		params.Status = &s
	}
	if opts.WorkRef != "" {
		w := opts.WorkRef
		params.WorkRef = &w
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Offset > 0 {
		o := opts.Offset
		params.Offset = &o
	}
	resp, err := client.ListApprovalsApiV1ApprovalsGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.ListApprovalsApiV1ApprovalsGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, nil
	}
	return *resp.JSON200, nil
}

// printListTable renders the list as a compact table.
// Columns: ID, STATUS, RUN, CONNECTOR, OP, PRINCIPAL, CREATED. RUN (#3543)
// says whether an approved request ran and how it ended, so a run that
// failed after the approval does not look like a plain "approved".
// statusFilter and offset are the --status and --offset values; an empty
// list names them, so an empty default (pending-only) list or an empty
// page past the end is not read as "no requests at all".
func printListTable(w io.Writer, items []api.ApprovalRequestView, statusFilter string, offset int) {
	if len(items) == 0 {
		fmt.Fprintln(w, emptyListMessage(statusFilter, offset))
		return
	}
	fmt.Fprintf(w, "%-36s %-9s %-11s %-24s %-32s %-20s %s\n",
		"ID", "STATUS", "RUN", "CONNECTOR", "OP", "PRINCIPAL", "CREATED")
	for _, e := range items {
		fmt.Fprintf(w, "%-36s %-9s %-11s %-24s %-32s %-20s %s\n",
			e.Id.String(),
			string(e.Status),
			runStateLabel(e.ResultState),
			truncate(e.ConnectorId, 24),
			truncate(e.OpId, 32),
			truncate(principalScanLabel(e.PrincipalSub, e.PrincipalName), 20),
			e.CreatedAt,
		)
	}
	fmt.Fprintf(w, "\nShowing %d.\n", len(items))
}

// emptyListMessage says which state filter and offset gave the empty
// list and how to see more. The server lists only pending requests when
// --status is not set. With an offset, the page is empty because the
// offset skipped every match, so the hint is a smaller --offset.
func emptyListMessage(statusFilter string, offset int) string {
	state := statusFilter + " "
	switch statusFilter {
	case "all":
		state = ""
	case "":
		state = "pending "
	}
	if offset > 0 {
		return fmt.Sprintf(
			"no %sapproval requests after skipping %d (this page is empty; use a smaller --offset)",
			state, offset,
		)
	}
	if statusFilter == "all" {
		return "no approval requests in this tenant"
	}
	return fmt.Sprintf("no %sapproval requests (use --status all to see every request)", state)
}

// truncate cuts s to maxLen runes, appending an ellipsis on truncation.
func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}
