// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCountCmd returns the `meho audit count` command (#4983).
//
//	meho audit count \
//	  [--target T] [--principal P] [--op-id PATTERN] [--op-class C]
//	  [--result-status S] [--since DUR] [--until DUR]
//	  [--audit-id ID] [--parent-audit-id ID] [--session-id ID]
//	  [--filter-work-ref REF]
//	  [--json] [--backplane <url>]
//
// Same filters as `meho audit query`, without --limit and --cursor: the
// backplane counts the whole matching set in one call. The work-ref
// filter is --filter-work-ref, so it does not hide the global
// --work-ref flag that tags the request itself.
//
// Exit codes match `meho audit query`.
func newCountCmd() *cobra.Command {
	var (
		opts    queryOptions
		jsonOut bool
	)
	cmd := &cobra.Command{
		Use:   "count",
		Short: "Count the audit rows that match a filter, per event class",
		Long: "count calls POST /api/v1/audit/class-counts and prints how many " +
			"audit rows match the filters: the total, the count per event class, " +
			"and the counted window. It takes the filters of `meho audit query` " +
			"and needs no paging. Without --since the backplane counts only the " +
			"last 24 hours; the output says which window it counted. --json " +
			"prints the backplane's answer as it came.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			opts.JSONOut = jsonOut
			return runCount(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringVar(&opts.Target, "target", "",
		"count only one target (name or alias; server-side resolution)")
	f.StringVar(&opts.Principal, "principal", "",
		"count only one operator (part of the user id or the name; case does not matter)")
	f.StringVar(&opts.OpID, "op-id", "",
		"count only one op-id (glob with * wildcards)")
	f.StringVar(&opts.OpClass, "op-class", "",
		"count only one op-class (read|write|credential_read|audit_query|other)")
	f.StringVar(&opts.ResultStatus, "result-status", "",
		"count only one result-status (ok|error|denied|pending)")
	f.StringVar(&opts.Since, "since", "",
		"start of the window; 24h / 7d / 30m / 2w shorthand or ISO-8601 (default: the last 24 hours)")
	f.StringVar(&opts.Until, "until", "",
		"end of the window; accepts the same shorthand as --since (default: now)")
	f.StringVar(&opts.AuditID, "audit-id", "",
		"count only this audit-id (UUID)")
	f.StringVar(&opts.ParentAuditID, "parent-audit-id", "",
		"count only the rows whose parent is this audit-id (UUID)")
	f.StringVar(&opts.SessionID, "session-id", "",
		"count only one agent session (UUID)")
	f.StringVar(&opts.WorkRef, "filter-work-ref", "",
		"count only the rows of one change-ticket reference (exact match; e.g. gh:evoila/meho#1)")
	f.BoolVar(&jsonOut, "json", false,
		"print the backplane's answer as it came instead of the summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runCount(cmd *cobra.Command, opts queryOptions) error {
	body, err := buildClassCountsRequest(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, result, err := postClassCounts(cmd.Context(), client, body)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) && he.statusCode == http.StatusNotFound {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("this backplane has no audit count route; it may be older than this CLI"),
				opts.JSONOut,
			)
		}
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditClassCounts"),
			opts.JSONOut,
		)
	}
	printCounts(cmd.OutOrStdout(), result, opts)
	return nil
}

// buildClassCountsRequest reuses the query body builder, so both
// commands check UUID filters the same way, and copies the filters
// the count route takes.
func buildClassCountsRequest(opts queryOptions) (api.AuditClassCountsRequest, error) {
	opts.Limit, opts.Cursor = 0, ""
	q, err := buildAuditQueryRequest(opts)
	if err != nil {
		return api.AuditClassCountsRequest{}, err
	}
	return api.AuditClassCountsRequest{
		AgentSessionId: q.AgentSessionId,
		AuditId:        q.AuditId,
		OpClass:        q.OpClass,
		OpId:           q.OpId,
		ParentAuditId:  q.ParentAuditId,
		Principal:      q.Principal,
		ResultStatus:   q.ResultStatus,
		Since:          q.Since,
		Target:         q.Target,
		Until:          q.Until,
		WorkRef:        q.WorkRef,
	}, nil
}

// postClassCounts calls the class-counts route with the one-shot 401
// refresh, like postQuery. It returns the raw body for --json and the
// decoded answer for the summary.
func postClassCounts(
	ctx context.Context,
	client *api.AuthedClient,
	body api.AuditClassCountsRequest,
) ([]byte, *api.AuditClassCounts, error) {
	resp, err := client.ClassCountsApiV1AuditClassCountsPostWithResponse(ctx, nil, body)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.ClassCountsApiV1AuditClassCountsPostWithResponse(ctx, nil, body)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// printCounts prints the counted window first, then the total and one
// line per event class (sorted by name, so the output is stable).
func printCounts(w io.Writer, r *api.AuditClassCounts, opts queryOptions) {
	from := formatTS(r.Since)
	if opts.Since == "" {
		from += " (the last 24 hours; set --since for a longer window)"
	}
	until := "now"
	if opts.Until != "" {
		until = opts.Until + " (as given to --until)"
	}
	fmt.Fprintf(w, "Counted from: %s\n", from)
	fmt.Fprintf(w, "Counted until: %s\n", until)
	fmt.Fprintf(w, "Total rows: %d\n", r.Total)
	classes := make([]string, 0, len(r.Counts))
	for c := range r.Counts {
		classes = append(classes, c)
	}
	sort.Strings(classes)
	fmt.Fprintf(w, "%-22s %s\n", "EVENT_CLASS", "ROWS")
	for _, c := range classes {
		fmt.Fprintf(w, "%-22s %d\n", c, r.Counts[c])
	}
}
