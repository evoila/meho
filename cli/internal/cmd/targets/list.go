// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// TargetSummary mirrors the backend TargetSummary Pydantic model
// (backend/src/meho_backplane/targets/schemas.py L57-72). Hand-
// written rather than aliased to the generated api.TargetSummary so
// the targets package stays decoupled from the generated client's
// type surface — the operation/retrieval packages take the same
// stance for the same reason (generated types churn on every spec
// re-snapshot).
type TargetSummary struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Aliases []string `json:"aliases"`
	Product string   `json:"product"`
	Host    string   `json:"host"`
}

// newListCmd returns the `meho targets list` command.
//
// CLI shape (matches issue #256 spec):
//
//	meho targets list \
//	  [--product P]                            # filter by product slug
//	  [--limit N]                              # 1..500, default 100
//	  [--cursor C]                             # keyset pagination cursor
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes mirror sibling verbs:
//   - 0   targets listed cleanly (including the empty-tenant case)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//   - 5   insufficient_role
func newListCmd() *cobra.Command {
	var (
		product           string
		limit             int
		cursor            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List targets in your tenant",
		Long: "list calls GET /api/v1/targets and renders the targets " +
			"registered in the operator's tenant. Optional --product " +
			"narrows by product slug (exact match). Results are keyset-" +
			"paginated by name; pass --cursor <last-name-seen> to fetch " +
			"the next page. --limit caps the page size (1..500, default " +
			"100). --json emits the raw API response so operators can " +
			"pipe into jq.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Product:           product,
				Limit:             limit,
				Cursor:            cursor,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVarP(&product, "product", "p", "",
		"filter by product slug (exact match)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max targets per page (1..500, server default 100 when omitted)")
	cmd.Flags().StringVar(&cursor, "cursor", "",
		"keyset pagination cursor (the last name from the previous page)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Product           string
	Limit             int
	Cursor            string
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	// Fail fast on out-of-range --limit. The API clamps internally
	// (FastAPI Query(ge=1, le=500)) but explicit zero/negative would
	// fall through with a 422 surprise. Surface the constraint here.
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	summaries, err := getTargets(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), summaries)
	}
	printTargetsTable(cmd.OutOrStdout(), summaries)
	return nil
}

// buildListPath assembles the GET /api/v1/targets query string from
// the per-call options. Exposed for tests so the URL construction
// stays unit-checkable without standing up an httptest.Server.
func buildListPath(opts listOptions) string {
	q := url.Values{}
	if opts.Product != "" {
		q.Set("product", opts.Product)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != "" {
		q.Set("cursor", opts.Cursor)
	}
	path := "/api/v1/targets"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getTargets(ctx context.Context, backplaneURL string, opts listOptions) ([]TargetSummary, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out []TargetSummary
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode targets response: %w", err)
	}
	return out, nil
}

// printTargetsTable renders the list as a compact, scannable table.
// Columns: NAME, ALIASES, PRODUCT, HOST per the issue's acceptance
// criterion 1. ID is omitted from the human view (operators rarely
// need the UUID; --json surfaces it).
func printTargetsTable(w io.Writer, summaries []TargetSummary) {
	if len(summaries) == 0 {
		fmt.Fprintln(w, "no targets registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-30s %-30s %-20s %s\n", "NAME", "ALIASES", "PRODUCT", "HOST")
	for _, s := range summaries {
		aliases := "-"
		if len(s.Aliases) > 0 {
			aliases = strings.Join(s.Aliases, ",")
		}
		fmt.Fprintf(w, "%-30s %-30s %-20s %s\n",
			truncate(s.Name, 30),
			truncate(aliases, 30),
			truncate(s.Product, 20),
			truncate(s.Host, 80),
		)
	}
}
