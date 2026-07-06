// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCollectionsListCmd returns the `meho docs collections list` command.
//
// G4.6-T4 (#1553). The catalogue-discovery verb: it lists the doc
// collections the operator is entitled to search (holds
// `meho-docs:<key>` for), mirroring `meho targets list`. It is the CLI
// sibling of the `list_doc_collections` MCP tool and the
// `GET /api/v1/doc_collections` REST route — an operator runs it to learn
// which `--collection` keys `meho docs search` will accept.
//
// Unlike the lifecycle verbs (probe / enable / disable, tenant_admin),
// `list` is a read every operator may run. There is no client-side
// capability gate (#2109): the backplane scopes the result to the
// collections the tenant is entitled to, server-side.
//
// CLI shape (mirrors `meho targets list`):
//
//	meho docs collections list \
//	  [--vendor V]                             # filter by vendor (exact match)
//	  [--limit N]                              # 1..500, server default 100
//	  [--cursor C]                             # keyset pagination cursor
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes mirror the sibling docs verbs:
//   - 0   collections listed cleanly (including the empty / unentitled case)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
//   - 5   insufficient_role
func newCollectionsListCmd() *cobra.Command {
	var (
		vendor            string
		limit             int
		cursor            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the doc collections you are entitled to search",
		Long: "list calls GET /api/v1/doc_collections and renders the " +
			"documentation collections the operator is entitled to search " +
			"(those the tenant holds a `meho-docs:<key>` capability for). " +
			"Each row carries the collection key — what `meho docs search " +
			"--collection` expects — plus the vendor, products, a " +
			"`when-to-use` blurb, and the cached liveness (status, doc " +
			"count, last ingest). Optional --vendor narrows by vendor " +
			"(exact match). Results are keyset-paginated by collection key; " +
			"pass --cursor <last-key-seen> to fetch the next page. --limit " +
			"caps the page size (1..500, server default 100). --json emits " +
			"the raw API response so operators can pipe into jq.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCollectionList(cmd, listCollectionsOptions{
				Vendor:            vendor,
				Limit:             limit,
				Cursor:            cursor,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&vendor, "vendor", "",
		"filter by vendor (exact match)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max collections per page (1..500, server default 100 when omitted)")
	cmd.Flags().StringVar(&cursor, "cursor", "",
		"keyset pagination cursor (the last collection key from the previous page)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// listCollectionsOptions is the flag/arg set for the catalogue list verb.
type listCollectionsOptions struct {
	Vendor            string
	Limit             int
	Cursor            string
	JSONOut           bool
	BackplaneOverride string
}

func runCollectionList(cmd *cobra.Command, opts listCollectionsOptions) error {
	// Fail fast on out-of-range --limit. The API clamps internally
	// (FastAPI Query(ge=1, le=500)) but an explicit zero/negative would
	// fall through to a 422 surprise; surface the constraint here.
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
	resp, err := listCollections(cmd.Context(), backplaneURL, opts)
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
				"call %s: HTTP 200 without a doc-collection list payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), *resp.JSON200)
	}
	printCollectionsTable(cmd.OutOrStdout(), *resp.JSON200)
	return nil
}

// buildListCollectionsParams assembles the typed query-parameter struct.
// Exposed for tests so the param wiring stays unit-checkable without
// standing up an httptest.Server; the typed client handles URL encoding.
func buildListCollectionsParams(
	opts listCollectionsOptions,
) *api.ListDocCollectionsEndpointApiV1DocCollectionsGetParams {
	params := &api.ListDocCollectionsEndpointApiV1DocCollectionsGetParams{}
	if opts.Vendor != "" {
		v := opts.Vendor
		params.Vendor = &v
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Cursor != "" {
		c := opts.Cursor
		params.Cursor = &c
	}
	return params
}

func listCollections(
	ctx context.Context,
	backplaneURL string,
	opts listCollectionsOptions,
) (*api.ListDocCollectionsEndpointApiV1DocCollectionsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := buildListCollectionsParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListDocCollectionsEndpointApiV1DocCollectionsGetResponse, error) {
			return authed.ListDocCollectionsEndpointApiV1DocCollectionsGetWithResponse(ctx, params)
		},
		func(r *api.ListDocCollectionsEndpointApiV1DocCollectionsGetResponse) int {
			return r.StatusCode()
		},
	)
}

// printCollectionsTable renders the catalogue as a compact, scannable
// table. Columns: KEY, VENDOR, PRODUCTS, STATUS, DOCS — the fields an
// operator reads to pick a collection (KEY) and judge its liveness
// (STATUS / DOCS). The UUID id and the timestamps are omitted from the
// human view; --json surfaces the full summary.
func printCollectionsTable(w io.Writer, collections []api.DocCollectionSummary) {
	if len(collections) == 0 {
		fmt.Fprintln(w, "no doc collections you are entitled to search")
		return
	}
	fmt.Fprintf(w, "%-20s %-24s %-24s %-14s %s\n", "KEY", "VENDOR", "PRODUCTS", "STATUS", "DOCS")
	for _, c := range collections {
		products := "-"
		if len(c.Products) > 0 {
			products = strings.Join(c.Products, ",")
		}
		fmt.Fprintf(w, "%-20s %-24s %-24s %-14s %s\n",
			truncate(c.CollectionKey, 20),
			truncate(c.Vendor, 24),
			truncate(products, 24),
			truncate(c.Status, 14),
			formatCollectionDocCount(c.DocCount),
		)
	}
}

// formatCollectionDocCount renders the optional cached doc count, which is
// *int on the wire (null until the first probe). A nil count renders as
// "-" so it isn't misread as 0.
func formatCollectionDocCount(count *int) string {
	if count == nil {
		return "-"
	}
	return fmt.Sprintf("%d", *count)
}
