// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// SearchHit mirrors the backend OperationSearchHit Pydantic model.
// summary / description / group_key / bm25_score / cosine_score are
// nullable per the backend's frozen pydantic model — pointer-typed
// here so the JSON unmarshal preserves the null vs. zero-value
// distinction.
//
// Kept hand-written for the same reason as GroupSummary (see
// groups.go): the FastAPI route types its response as
// `dict[str, Any]`, so the oapi-codegen generator emits no
// `OperationSearchHit` model worth using. Promoting the FastAPI
// response to a typed model so the generator picks it up is a
// separate backend Task explicitly out of scope for G0.12-T2 #1260.
type SearchHit struct {
	OpID             string   `json:"op_id"`
	Summary          *string  `json:"summary"`
	Description      *string  `json:"description"`
	GroupKey         *string  `json:"group_key"`
	SafetyLevel      string   `json:"safety_level"`
	RequiresApproval bool     `json:"requires_approval"`
	FusedScore       float64  `json:"fused_score"`
	Bm25Score        *float64 `json:"bm25_score"`
	CosineScore      *float64 `json:"cosine_score"`
}

// SearchResponse is the JSON envelope returned by
// GET /api/v1/operations/search. Hand-typed for the same reason as
// SearchHit above.
type SearchResponse struct {
	Hits            []SearchHit `json:"hits"`
	QueryDurationMs float64     `json:"query_duration_ms"`
}

// newSearchCmd returns the `meho operation search` command.
//
// CLI shape:
//
//	meho operation search <connector_id> "<query>" \
//	  [--group <key>]                          # narrow within one group
//	  [--limit N]                              # 1..50, default 10
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes mirror groups + retrieval/eval.
func newSearchCmd() *cobra.Command {
	var (
		groupKey          string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <connector_id> <query>",
		Short: "Hybrid BM25 + cosine RRF search across enabled operations",
		Long: "search calls GET /api/v1/operations/search with the supplied " +
			"query string against the named connector_id. The backplane runs " +
			"hybrid BM25 + cosine retrieval with Reciprocal Rank Fusion and " +
			"returns the top `--limit` hits (default 10, clamped at 50 by " +
			"the API). --group narrows to one group_key within the connector " +
			"(useful when the connector exposes many groups and the query is " +
			"ambiguous across them).",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSearch(cmd, searchOptions{
				ConnectorID:       args[0],
				Query:             args[1],
				GroupKey:          groupKey,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&groupKey, "group", "",
		"narrow the search to one group_key within the connector")
	cmd.Flags().IntVar(&limit, "limit", 10,
		"max hits to return (1..50, clamped by the API at 50)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type searchOptions struct {
	ConnectorID       string
	Query             string
	GroupKey          string
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runSearch(cmd *cobra.Command, opts searchOptions) error {
	// Fail fast on out-of-range --limit. The API clamps at 50 on the
	// upper bound (so passing 100 silently lands 50; documented), but
	// negative / zero values would fall through to the API default and
	// surprise operators who explicitly asked for "no results please".
	if opts.Limit < 1 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 1; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result, err := getSearch(cmd.Context(), client, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printSearchTable(cmd.OutOrStdout(), opts.ConnectorID, opts.Query, result)
	return nil
}

// getSearch issues the typed GET via the generated client. The
// generated GetSearchApiV1OperationsSearchGetParams carries typed
// pointer fields for the optional query params (Q, Group, Limit) — the
// generator emits them as *string / *int so a nil value omits the
// param from the URL. ConnectorId is required and stays a plain
// string; the free-text query is sent via the canonical `q` param
// (#1854; the legacy `query` alias is deprecated). `q` is a pointer on
// the wire but the CLI always supplies a non-empty value.
func getSearch(ctx context.Context, client operationsAPI, opts searchOptions) (*SearchResponse, error) {
	params := &api.GetSearchApiV1OperationsSearchGetParams{
		ConnectorId: opts.ConnectorID,
		Q:           &opts.Query,
	}
	if opts.GroupKey != "" {
		gk := opts.GroupKey
		params.Group = &gk
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	resp, err := client.GetSearchApiV1OperationsSearchGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.GetSearchApiV1OperationsSearchGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out SearchResponse
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode search response: %w", err)
	}
	return &out, nil
}

func printSearchTable(w io.Writer, connectorID, query string, r *SearchResponse) {
	fmt.Fprintf(w, "search %s %q — %d hit(s) in %.0fms\n",
		connectorID, query, len(r.Hits), r.QueryDurationMs)
	if len(r.Hits) == 0 {
		return
	}
	fmt.Fprintf(w, "%-40s %6s  %s\n", "op_id", "score", "summary")
	for _, h := range r.Hits {
		fmt.Fprintf(w, "%-40s %6.3f  %s\n",
			truncate(h.OpID, 40),
			h.FusedScore,
			truncate(strDeref(h.Summary), 80),
		)
	}
}

// strDeref returns *s or empty string when s is nil. The backend's
// frozen pydantic SearchHit declares summary / description /
// group_key as Optional[str]; a typed connector with no summary on
// a registered op surfaces as JSON null, which lands as nil here.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
