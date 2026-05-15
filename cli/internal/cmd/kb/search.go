// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// retrieveRequest mirrors the backend RetrieveRequest pydantic
// model. The kb `search` verb pins `source="kb"` so a search call
// from this surface is always kb-scoped; the underlying
// /api/v1/retrieve route supports cross-source retrieval but the
// kb CLI deliberately constrains it (operators searching the
// memory layer use `meho recall`, agents use the meta-tools).
type retrieveRequest struct {
	Query  string `json:"query"`
	Source string `json:"source,omitempty"`
	Kind   string `json:"kind,omitempty"`
	Limit  int    `json:"limit,omitempty"`
}

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
	Query             string
	Limit             int
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
	if opts.Limit < 0 || opts.Limit > 50 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 50; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	resp, err := postSearch(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printSearchTable(cmd.OutOrStdout(), resp)
	return nil
}

func postSearch(ctx context.Context, backplaneURL string, opts searchOptions) (*RetrieveResponse, error) {
	body := retrieveRequest{
		Query:  opts.Query,
		Source: "kb",
	}
	if opts.Limit > 0 {
		body.Limit = opts.Limit
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal kb search request: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/retrieve", raw)
	if err != nil {
		return nil, err
	}
	var out RetrieveResponse
	if err := json.Unmarshal(resp, &out); err != nil {
		return nil, fmt.Errorf("decode kb search response: %w", err)
	}
	return &out, nil
}

// printSearchTable renders the ranked hits as a compact table.
// Columns: RANK (1-based), SCORE (fused), SLUG (the kb identifier
// — the substrate's `source_id`), SNIPPET (200-char excerpt of the
// body). The hit's `kind` is always `kb-entry` (the substrate
// scopes via source filter); rendering it would be noise.
func printSearchTable(w io.Writer, r *RetrieveResponse) {
	if r == nil || len(r.Hits) == 0 {
		fmt.Fprintln(w, "no kb hits for this query")
		return
	}
	fmt.Fprintf(w, "%-5s %-8s %-40s %s\n", "RANK", "SCORE", "SLUG", "SNIPPET")
	for i, hit := range r.Hits {
		fmt.Fprintf(w, "%-5d %-8.4f %-40s %s\n",
			i+1,
			hit.FusedScore,
			truncate(hit.SourceID, 40),
			truncate(snippetOf(hit.Body), 80),
		)
	}
	if r.QueryDurationMS > 0 {
		fmt.Fprintf(w, "queried in %.2f ms\n", r.QueryDurationMS)
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
