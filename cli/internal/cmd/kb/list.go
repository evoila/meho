// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

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

// newListCmd returns the `meho kb list` command.
//
// CLI shape (per issue #418):
//
//	meho kb list \
//	  [--filter PATTERN]   # SQL LIKE pattern forwarded to the substrate
//	  [--limit N]          # 1..500, default 100 (server-side)
//	  [--offset N]         # offset-based pagination, default 0
//	  [--json]             # raw KbListResponse JSON instead of the table
//	  [--backplane <url>]  # override the configured backplane URL
//
// Exit codes:
//   - 0   list returned cleanly (including zero rows)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//   - 5   insufficient_role
func newListCmd() *cobra.Command {
	var (
		filter            string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List kb entries in your tenant",
		Long: "list calls GET /api/v1/kb and renders the kb entries " +
			"registered in the operator's tenant, slug-sorted. " +
			"Optional --filter narrows by a SQL LIKE pattern (the " +
			"operator is the trust boundary for pattern shape). " +
			"--limit caps the page size (1..500, server default 100). " +
			"--offset advances the page window (default 0). --json " +
			"emits the raw KbListResponse envelope for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Filter:            filter,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&filter, "filter", "",
		"narrow entries by a SQL LIKE pattern (e.g. `vcenter%`)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max entries per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the slug-sorted result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw KbListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Filter            string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	// Fail fast on out-of-range --limit / --offset. The backend
	// clamps internally (Query(ge=1, le=500) on limit, Query(ge=0)
	// on offset) but explicit zero / negative would fall through
	// with a 422 surprise.
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
	resp, err := getList(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	// printListTable's nil-or-empty branch prints "no kb entries
	// registered in this tenant" — without this guard, a malformed
	// 200 would be actively misleading (conflated with a
	// genuinely-empty tenant). Mirrors the convention in
	// `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a kb list payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param
// shape. Each pointer field is set only when the operator supplied
// the flag so the backplane's own defaults apply for unset values
// (filter omitted entirely; limit defaults to 100 server-side;
// offset defaults to 0).
func listQueryParams(opts listOptions) *api.ListKbApiV1KbGetParams {
	params := &api.ListKbApiV1KbGetParams{}
	if opts.Filter != "" {
		f := opts.Filter
		params.Filter = &f
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

func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
) (*api.ListKbApiV1KbGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListKbApiV1KbGetResponse, error) {
			return authed.ListKbApiV1KbGetWithResponse(ctx, params)
		},
		func(r *api.ListKbApiV1KbGetResponse) int { return r.StatusCode() },
	)
}

// printListTable renders the list as a compact, scannable table.
// Columns: SLUG, UPDATED, PREVIEW. The full ISO-8601 timestamp is
// kept verbatim (not truncated) because operators correlating with
// audit-log rows want the precise updated_at; the column width is
// 32 chars, sized for the worst-case Python `datetime.isoformat()`
// shape `YYYY-MM-DDTHH:MM:SS.ffffff+HH:MM`. The preview column
// carries an 80-char excerpt of the 200-char backend preview so a
// default terminal width doesn't wrap.
func printListTable(w io.Writer, r *api.KbListResponse) {
	if r == nil || len(r.Entries) == 0 {
		fmt.Fprintln(w, "no kb entries registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-40s %-32s %s\n", "SLUG", "UPDATED", "PREVIEW")
	for _, e := range r.Entries {
		fmt.Fprintf(w, "%-40s %-32s %s\n",
			truncate(e.Slug, 40),
			e.UpdatedAt,
			truncate(e.Preview, 80),
		)
	}
}
