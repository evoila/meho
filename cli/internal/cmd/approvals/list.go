// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho approvals list` command.
//
//	meho approvals list [--status pending] [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Lists approval requests for the operator's tenant,
// newest-first, via GET /api/v1/approvals.
func newListCmd() *cobra.Command {
	var (
		statusFilter      string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List approval requests in your tenant",
		Long: "list calls GET /api/v1/approvals and renders approval " +
			"requests in the operator's tenant, newest first. Use " +
			"--status pending for the most common query: requests " +
			"awaiting a decision. --limit caps the page size " +
			"(1..500, server default 50). --offset advances the page " +
			"window. --json emits the raw JSON array for " +
			"jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOpts{
				StatusFilter:      statusFilter,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&statusFilter, "status", "",
		"filter by status: pending, approved, rejected, expired (default: all)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max requests per page (1..500, server default 50 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw JSON array instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

type listOpts struct {
	StatusFilter      string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOpts) error {
	validStatuses := map[string]bool{
		"pending": true, "approved": true, "rejected": true, "expired": true,
	}
	if opts.StatusFilter != "" && !validStatuses[opts.StatusFilter] {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--status must be one of: pending, approved, rejected, expired; got %q",
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
	resp, err := fetchList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	return nil
}

// buildListPath assembles the GET /api/v1/approvals query string.
func buildListPath(opts listOpts) string {
	q := url.Values{}
	if opts.StatusFilter != "" {
		q.Set("status", opts.StatusFilter)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Offset > 0 {
		q.Set("offset", strconv.Itoa(opts.Offset))
	}
	path := "/api/v1/approvals"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func fetchList(ctx context.Context, backplaneURL string, opts listOpts) ([]ApprovalSummary, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out []ApprovalSummary
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode approvals list response: %w", err)
	}
	return out, nil
}

// printListTable renders the list as a compact table.
// Columns: ID (truncated), STATUS, CONNECTOR, OP, PRINCIPAL, CREATED.
func printListTable(w io.Writer, items []ApprovalSummary) {
	if len(items) == 0 {
		fmt.Fprintln(w, "no approval requests in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-9s %-24s %-32s %-20s %s\n",
		"ID", "STATUS", "CONNECTOR", "OP", "PRINCIPAL", "CREATED")
	for _, e := range items {
		fmt.Fprintf(w, "%-36s %-9s %-24s %-32s %-20s %s\n",
			e.ID,
			e.Status,
			truncate(e.ConnectorID, 24),
			truncate(e.OpID, 32),
			truncate(e.PrincipalSub, 20),
			e.CreatedAt,
		)
	}
	fmt.Fprintf(w, "\nShowing %d.\n", len(items))
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
