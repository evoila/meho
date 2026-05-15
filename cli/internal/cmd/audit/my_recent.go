// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newMyRecentCmd returns the `meho audit my-recent` command.
//
// CLI shape (per issue #467):
//
//	meho audit my-recent [--since DUR] [--limit N] [--json] [--backplane <url>]
//
// Calls GET /api/v1/audit/my-recent. The backend reads the operator's
// `sub` claim from the verified JWT and binds it as the `principal`
// filter — there is no surface that accepts an operator override.
// Cross-operator inspection is `meho audit query --principal P`.
//
// Exit codes mirror `meho audit query`.
func newMyRecentCmd() *cobra.Command {
	var (
		since             string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "my-recent",
		Short: "Show your own recent audit activity",
		Long: "my-recent calls GET /api/v1/audit/my-recent and renders the " +
			"audit rows the calling operator produced. The `principal` " +
			"filter is taken from the JWT's `sub` claim server-side — " +
			"there is no surface that accepts an operator override. " +
			"--since defaults to 24h server-side; pass a different " +
			"shorthand (7d / 30m / 2w) or an ISO-8601 datetime to widen " +
			"the window. --limit caps the page size (1..1000, server " +
			"default 100). --json emits the raw QueryResult.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runMyRecent(cmd, myRecentOptions{
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

type myRecentOptions struct {
	Since             string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runMyRecent(cmd *cobra.Command, opts myRecentOptions) error {
	if opts.Limit < 0 || opts.Limit > 1000 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 1000; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := getMyRecent(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printQueryTable(cmd.OutOrStdout(), result)
	return nil
}

// buildMyRecentPath assembles the GET path with optional --since /
// --limit query params. Exposed for unit tests.
func buildMyRecentPath(since string, limit int) string {
	q := url.Values{}
	if since != "" {
		q.Set("since", since)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	path := "/api/v1/audit/my-recent"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getMyRecent(ctx context.Context, backplaneURL string, opts myRecentOptions) (*QueryResult, error) {
	raw, err := doAuthedRequest(
		ctx, backplaneURL, "GET",
		buildMyRecentPath(opts.Since, opts.Limit), nil,
	)
	if err != nil {
		return nil, err
	}
	var out QueryResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode my-recent response: %w", err)
	}
	return &out, nil
}
