// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"fmt"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newWhoTouchedCmd returns the `meho audit who-touched <target>`
// command.
//
// CLI shape (per issue #467):
//
//	meho audit who-touched <target> \
//	  [--since DUR]    # 24h | 7d | ISO-8601, default 24h
//	  [--limit N]      # 1..1000, server default 100
//	  [--json]
//	  [--backplane <url>]
//
// Calls GET /api/v1/audit/who-touched/{target}. The backend resolves
// the target name against the same-tenant `targets` table; an
// unmatched name returns an empty result (not an error).
//
// Exit codes mirror `meho audit query`.
func newWhoTouchedCmd() *cobra.Command {
	var (
		since             string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "who-touched <target>",
		Short: "Show every audit row that touched a specific target",
		Long: "who-touched calls GET /api/v1/audit/who-touched/{target} " +
			"and renders the audit rows where target_name matches the " +
			"argument inside the operator's tenant. --since defaults to " +
			"24h; pass a different shorthand (7d / 30m / 2w) or an " +
			"ISO-8601 datetime to widen the window. --limit caps the " +
			"page size (1..1000, server default 100). --json emits the " +
			"raw QueryResult.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runWhoTouched(cmd, whoTouchedOptions{
				Target:            args[0],
				Since:             since,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&since, "since", "",
		"earliest occurred_at; defaults server-side to 24h when omitted")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows (1..1000, server default 100 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw QueryResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type whoTouchedOptions struct {
	Target            string
	Since             string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runWhoTouched(cmd *cobra.Command, opts whoTouchedOptions) error {
	if opts.Target == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("who-touched requires a non-empty <target> argument"),
			opts.JSONOut,
		)
	}
	if opts.Limit < 0 || opts.Limit > 1000 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 1000; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, err := getWhoTouched(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printQueryTable(cmd.OutOrStdout(), result)
	return nil
}

// buildWhoTouchedPath assembles the GET path with query params. Only
// emits the query params the operator set so the backend's own
// defaults (since=24h, limit=100) take over otherwise. Exposed for
// unit tests so the URL encoding of names with special characters
// stays covered.
func buildWhoTouchedPath(target string, since string, limit int) string {
	q := url.Values{}
	if since != "" {
		q.Set("since", since)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	path := "/api/v1/audit/who-touched/" + pathEscape(target)
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getWhoTouched(ctx context.Context, backplaneURL string, opts whoTouchedOptions) (*QueryResult, error) {
	raw, err := doAuthedRequest(
		ctx, backplaneURL, "GET",
		buildWhoTouchedPath(opts.Target, opts.Since, opts.Limit), nil,
	)
	if err != nil {
		return nil, err
	}
	var out QueryResult
	if err := decodeAuditResponse(raw, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
