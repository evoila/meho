// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

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

// newListCmd returns the `meho scheduler list` command.
//
//	meho scheduler list [--kind K] [--status S] [--tenant T]
//	                    [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Operator role is scoped to its own tenant; --tenant is
// a tenant_admin-only filter (the backend returns 403 for an operator
// who passes it).
func newListCmd() *cobra.Command {
	var (
		kind              string
		status            string
		tenant            string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List scheduled triggers in your tenant",
		Long: "list calls GET /api/v1/scheduler/triggers and renders " +
			"the triggers in the operator's tenant, newest-first. " +
			"--kind narrows to cron|one_off|event; --status narrows to " +
			"active|paused|cancelled|fired. --tenant is a tenant_admin-" +
			"only filter that targets another tenant; operator role " +
			"calling with --tenant lands as 403 insufficient_role. " +
			"--limit caps the page size (1..500, server default 100). " +
			"--offset advances the page window (default 0). --json emits " +
			"the raw ListResponse envelope for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Kind:              kind,
				Status:            status,
				Tenant:            tenant,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "",
		"filter by trigger kind: cron | one_off | event")
	cmd.Flags().StringVar(&status, "status", "",
		"filter by trigger status: active | paused | cancelled | fired")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (tenant_admin only; operator role is locked to its own tenant)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max triggers per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Kind              string
	Status            string
	Tenant            string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.Kind != "" && !validKinds[opts.Kind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--kind must be one of: cron, one_off, event"), opts.JSONOut)
	}
	if opts.Status != "" && !validStatuses[opts.Status] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--status must be one of: active, paused, cancelled, fired"),
			opts.JSONOut)
	}
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut)
	}
	if opts.Offset < 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be non-negative; got %d", opts.Offset)),
			opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printListTable(cmd.OutOrStdout(), resp)
	return nil
}

// buildListPath assembles the GET /api/v1/scheduler/triggers query
// string. Exposed for unit tests so URL construction stays checkable.
func buildListPath(opts listOptions) string {
	q := url.Values{}
	if opts.Kind != "" {
		q.Set("kind", opts.Kind)
	}
	if opts.Status != "" {
		q.Set("status", opts.Status)
	}
	if opts.Tenant != "" {
		q.Set("tenant_filter", opts.Tenant)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Offset > 0 {
		q.Set("offset", strconv.Itoa(opts.Offset))
	}
	path := "/api/v1/scheduler/triggers"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getList(ctx context.Context, backplaneURL string, opts listOptions) (*ListResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out ListResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode scheduler list response: %w", err)
	}
	return &out, nil
}

// printListTable renders the triggers as a compact table:
// ID, KIND, STATUS, SCHEDULE (cron_expr or fire_at), NEXT_FIRE_AT.
func printListTable(w io.Writer, r *ListResponse) {
	if r == nil || len(r.Triggers) == 0 {
		fmt.Fprintln(w, "no scheduled triggers in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-8s %-10s %-30s %s\n",
		"ID", "KIND", "STATUS", "SCHEDULE", "NEXT_FIRE_AT")
	for _, t := range r.Triggers {
		schedule := ""
		switch t.Kind {
		case "cron":
			if t.CronExpr != nil {
				schedule = *t.CronExpr
				if t.Timezone != "" && t.Timezone != "UTC" {
					schedule = schedule + " (" + t.Timezone + ")"
				}
			}
		case "one_off":
			if t.FireAt != nil {
				schedule = *t.FireAt
			}
		case "event":
			schedule = "event-filter"
		}
		next := "-"
		if t.NextFireAt != nil {
			next = *t.NextFireAt
		}
		fmt.Fprintf(w, "%-36s %-8s %-10s %-30s %s\n",
			t.ID, t.Kind, t.Status, schedule, next)
	}
}
