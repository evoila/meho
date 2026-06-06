// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

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

// newSearchCmd returns the `meho docs search` command.
//
// CLI shape (per issue #1552):
//
//	meho docs search <query> --collection <c> \
//	  [--product <p>] [--version <v>] [--limit N] [--json] [--backplane <url>]
//
// Role: operator. Calls POST /api/v1/search_docs (T3, #1552) via the
// shared authed client (bearer + 401-refresh). `provisioned` carries
// the meho-docs capability gate resolved at command-tree-build time;
// when false the verb refuses with a typed `addon_not_provisioned`
// error before any flag validation or network call.
//
// --collection is the mandatory binary scope: it routes the query to a
// backend and gates per-collection entitlement. The CLI fails fast on a
// missing --collection (exit 4) before the round-trip, mirroring the
// route's 422 rather than incurring it. --product / --version are
// optional refinements within the collection.
//
// Exit codes:
//   - 0   search returned cleanly (incl. zero-hit result)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (missing --collection, out-of-range
//     --limit, a 422/503 from the route)
//   - 5   insufficient_role / addon_not_provisioned
func newSearchCmd(provisioned bool) *cobra.Command {
	var (
		collection        string
		product           string
		version           string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Search a vendor-document collection (mandatory --collection)",
		Long: "search calls POST /api/v1/search_docs and renders the " +
			"ranked cited chunks as a text table. --collection is " +
			"mandatory: it is the binary scope that routes the query to a " +
			"collection's backend and gates per-collection entitlement (a " +
			"docs query without it is rejected rather than run unscoped). " +
			"--product and --version are optional refinements within the " +
			"collection. The query routes through the backplane so it " +
			"is audited centrally; the raw query is never logged (only " +
			"its hash). --json emits the raw SearchDocsResponse with the " +
			"full DocsChunk shape (chunk id, document id, score, source " +
			"url).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSearch(cmd, searchOptions{
				Query:             args[0],
				Collection:        collection,
				Product:           product,
				Version:           version,
				Limit:             limit,
				Changed:           cmd.Flags().Changed("limit"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				Provisioned:       provisioned,
			})
		},
	}
	cmd.Flags().StringVar(&collection, "collection", "",
		"collection key to search (required; e.g. vmware)")
	cmd.Flags().StringVar(&product, "product", "",
		"optional vendor-product refinement within the collection")
	cmd.Flags().StringVar(&version, "version", "",
		"optional product-version refinement within the collection")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max chunks to return (1..50, server default 10 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw SearchDocsResponse JSON (full DocsChunk shape)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type searchOptions struct {
	Query      string
	Collection string
	Product    string
	Version    string
	Limit      int
	// Changed mirrors `cobra.Command.Flags().Changed("limit")` for the
	// "operator-supplied 0 is an error; default-0 means 'use the
	// server default'" gate. Threaded as a field rather than re-read
	// off cmd so tests that drive runSearch directly (bypassing the
	// cobra flag-parse path) can opt the gate in / out explicitly.
	Changed           bool
	JSONOut           bool
	BackplaneOverride string
	// Provisioned carries the meho-docs capability gate. When false,
	// runSearch refuses with the typed addon_not_provisioned error
	// before touching flags or the network.
	Provisioned bool
}

func runSearch(cmd *cobra.Command, opts searchOptions) error {
	// Capability gate first: an unprovisioned tenant must not be able
	// to reach the flag validation, let alone the corpus. This closes
	// the "Hidden but still invokable" gap cobra's Hidden leaves —
	// the command is hidden from --help AND refuses when invoked by
	// path.
	if !opts.Provisioned {
		return errNotProvisioned(cmd, opts.JSONOut)
	}
	if opts.Query == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("search requires a non-empty <query> argument"),
			opts.JSONOut,
		)
	}
	// Collection scope, client-side: --collection is the mandatory
	// binary scope. Fail fast with the same constraint the route would
	// 422 on, so operators see it locally without a round-trip.
	// --product / --version are optional refinements (no client gate).
	if opts.Collection == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("search requires --collection (the mandatory binary scope)"),
			opts.JSONOut,
		)
	}
	// Fail fast on out-of-range --limit. The backend clamps with
	// Field(ge=1, le=50); the CLI mirrors the bound. Zero is cobra's
	// default for an unset IntVar — preserve that as the "no flag"
	// sentinel (buildSearchBody omits the field on zero), but reject
	// an explicit --limit=0 (outside the documented 1..50 range).
	if opts.Limit < 0 || opts.Limit > 50 || (opts.Changed && opts.Limit == 0) {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 50 when provided; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := searchDocs(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil. A
	// malformed 200 must not be conflated with a genuinely-empty
	// result set (which printSearchTable renders as "no docs hits").
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a search_docs response payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printSearchTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// buildSearchBody assembles the typed POST body for /api/v1/search_docs.
// collection is the mandatory binary scope (non-empty by the time this is
// reached); product/version are optional refinements and only land on the
// wire when the operator supplied them (the generated field tags are
// `omitempty`, so a nil pointer keeps the JSON key absent and the backend
// treats the refinement as unset). Limit only lands when the operator
// passed a positive --limit — same omitempty contract, so the backend's
// Field(ge=1, le=50, default=10) applies on absence.
func buildSearchBody(opts searchOptions) api.SearchDocsRequest {
	collection := opts.Collection
	body := api.SearchDocsRequest{
		Query:      opts.Query,
		Collection: &collection,
	}
	if opts.Product != "" {
		product := opts.Product
		body.Product = &product
	}
	if opts.Version != "" {
		version := opts.Version
		body.Version = &version
	}
	if opts.Limit > 0 {
		limit := opts.Limit
		body.Limit = &limit
	}
	return body
}

func searchDocs(
	ctx context.Context,
	backplaneURL string,
	opts searchOptions,
) (*api.SearchDocsEndpointApiV1SearchDocsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildSearchBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.SearchDocsEndpointApiV1SearchDocsPostResponse, error) {
			return authed.SearchDocsEndpointApiV1SearchDocsPostWithResponse(
				ctx,
				&api.SearchDocsEndpointApiV1SearchDocsPostParams{},
				reqBody,
			)
		},
		func(r *api.SearchDocsEndpointApiV1SearchDocsPostResponse) int { return r.StatusCode() },
	)
}

// printSearchTable renders the ranked cited chunks as a compact table.
// Columns: RANK (1-based), SCORE (corpus score; "-" when the corpus
// omitted it), DOCUMENT (the document id citation), SNIPPET (200-char
// excerpt of the chunk content). The chunk id and source url are
// available in --json output for citation drill-down; rendering them
// in the table would overflow a default terminal width.
func printSearchTable(w io.Writer, r *api.SearchDocsResponse) {
	if r == nil || len(r.Chunks) == 0 {
		fmt.Fprintln(w, "no docs hits for this query")
		return
	}
	fmt.Fprintf(w, "%-5s %-8s %-40s %s\n", "RANK", "SCORE", "DOCUMENT", "SNIPPET")
	for i, chunk := range r.Chunks {
		fmt.Fprintf(w, "%-5d %-8s %-40s %s\n",
			i+1,
			formatScore(chunk.Score),
			truncate(chunk.DocumentId, 40),
			truncate(snippetOf(chunk.Content), 80),
		)
	}
}

// formatScore renders the corpus score, which is optional on the wire
// (DocsChunk.Score is a *float32). A nil score renders as "-" so the
// column stays aligned and an absent score isn't misread as 0.0000.
func formatScore(score *float32) string {
	if score == nil {
		return "-"
	}
	return fmt.Sprintf("%.4f", *score)
}

// snippetOf returns the first ~200 chars of content so the table
// render fits a default terminal width. Kept separate from truncate
// (which trims to a final length with an ellipsis) so the snippet
// width is independent of the truncate(maxLen) contract used for the
// other columns.
func snippetOf(content string) string {
	const snippetChars = 200
	runes := []rune(content)
	if len(runes) <= snippetChars {
		return content
	}
	return string(runes[:snippetChars]) + "…"
}
