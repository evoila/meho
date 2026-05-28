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

// newSearchCmd returns the `meho kb search` command.
//
// CLI shape (per issue #418):
//
//	meho kb search <query> [--limit N] [--json] [--backplane <url>]
//
// Role: operator. Pins `source="kb"` server-side so the retrieve
// substrate scopes hits to kb-entry rows.
//
// Exit codes:
//   - 0   search returned cleanly (incl. zero-hit result)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 422 query-too-long /
//     query-empty; the backend caps query length at 2000 chars and
//     limit at 50)
//   - 5   insufficient_role
func newSearchCmd() *cobra.Command {
	var (
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Search kb entries via hybrid BM25 + cosine retrieval",
		Long: "search calls POST /api/v1/retrieve with source=\"kb\" " +
			"and renders the ranked hits as a text table. Hybrid " +
			"BM25 + cosine retrieval with RRF fusion is applied by " +
			"the substrate; per-signal scores and ranks are visible " +
			"in --json output for retrieval tuning. The query is " +
			"capped at 2000 chars and the result limit at 50 by the " +
			"backend (G0.4-T5 contract). The route audits the query " +
			"hash but not the raw query (decision #3 in " +
			"docs/planning/v0.2-decisions.md).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSearch(cmd, searchOptions{
				Query:             args[0],
				Limit:             limit,
				Changed:           cmd.Flags().Changed("limit"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max hits to return (1..50, server default 10 when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw RetrieveResponse JSON (full RetrievalHit shape)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type searchOptions struct {
	Query string
	Limit int
	// Changed mirrors `cobra.Command.Flags().Changed("limit")` for
	// runSearch's "operator-supplied 0 is an error; default-0 means
	// 'use the server default'" gate. Threaded as a field rather
	// than re-reading off cmd so tests that drive runSearch directly
	// (bypassing the cobra flag-parse path) can opt in / out of
	// the gate explicitly.
	Changed           bool
	JSONOut           bool
	BackplaneOverride string
}

func runSearch(cmd *cobra.Command, opts searchOptions) error {
	if opts.Query == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("search requires a non-empty <query> argument"),
			opts.JSONOut,
		)
	}
	// Fail fast on out-of-range --limit. The backend clamps with
	// Field(ge=1, le=50); the CLI mirrors the bound so operators
	// see the constraint string locally instead of a 422 round-trip.
	// Zero is cobra's default for an unset IntVar — preserve that as
	// the "no flag" sentinel (`buildSearchBody` omits the field on
	// zero), but reject an *explicit* `--limit=0` (loud failure for
	// a value outside the documented 1..50 range).
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
	resp, err := searchKb(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	// printSearchTable's nil-or-empty branch prints "no kb hits for
	// this query" — without this guard, a malformed 200 would be
	// actively misleading (conflated with a genuinely-empty result
	// set). Mirrors the convention in `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a retrieve response payload",
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

// buildSearchBody assembles the typed POST body for /api/v1/retrieve.
// `Source` is pinned to "kb" so the substrate scopes hits to kb-entry
// rows. `Limit` only flows onto the wire when the operator passed a
// positive `--limit` — the generated field tag is `omitempty`, so a
// nil pointer keeps the JSON key absent and the backend's `Field(ge=1,
// le=50, default=10)` applies. `Kind` and `MetadataFilters` stay nil
// for the kb search verb; the operator surface doesn't expose either
// in v0.2 (memory's search verb adds metadata_filters under G4.4-T2).
func buildSearchBody(opts searchOptions) api.RetrieveRequest {
	src := "kb"
	body := api.RetrieveRequest{
		Query:  opts.Query,
		Source: &src,
	}
	if opts.Limit > 0 {
		limit := opts.Limit
		body.Limit = &limit
	}
	return body
}

func searchKb(
	ctx context.Context,
	backplaneURL string,
	opts searchOptions,
) (*api.RetrieveEndpointApiV1RetrievePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	reqBody := buildSearchBody(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RetrieveEndpointApiV1RetrievePostResponse, error) {
			return authed.RetrieveEndpointApiV1RetrievePostWithResponse(
				ctx,
				&api.RetrieveEndpointApiV1RetrievePostParams{},
				reqBody,
			)
		},
		func(r *api.RetrieveEndpointApiV1RetrievePostResponse) int { return r.StatusCode() },
	)
}

// printSearchTable renders the ranked hits as a compact table.
// Columns: RANK (1-based), SCORE (fused), SLUG (the kb identifier
// — the substrate's `source_id`), SNIPPET (200-char excerpt of the
// body). The hit's `kind` is always `kb-entry` (the substrate
// scopes via source filter); rendering it would be noise.
func printSearchTable(w io.Writer, r *api.RetrieveResponse) {
	if r == nil || len(r.Hits) == 0 {
		fmt.Fprintln(w, "no kb hits for this query")
		return
	}
	fmt.Fprintf(w, "%-5s %-8s %-40s %s\n", "RANK", "SCORE", "SLUG", "SNIPPET")
	for i, hit := range r.Hits {
		fmt.Fprintf(w, "%-5d %-8.4f %-40s %s\n",
			i+1,
			hit.FusedScore,
			truncate(hit.SourceId, 40),
			truncate(snippetOf(hit.Body), 80),
		)
	}
	if r.QueryDurationMs > 0 {
		fmt.Fprintf(w, "queried in %.2f ms\n", r.QueryDurationMs)
	}
}

// snippetOf returns the first ~200 chars of body so the table render
// fits a default terminal width. Pulled into a helper because the
// truncate helper already trims to a final length — using a separate
// snippet step keeps the search-table column widths independent of
// the truncate(maxLen) contract used elsewhere.
func snippetOf(body string) string {
	const snippetChars = 200
	runes := []rune(body)
	if len(runes) <= snippetChars {
		return body
	}
	return string(runes[:snippetChars]) + "…"
}
