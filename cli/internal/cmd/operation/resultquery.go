// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// ResultQueryResult mirrors the backend result-query envelope — the
// `dict[str, Any]` returned by POST /api/v1/operations/result-query
// (read_result_window). Hand-written for the same reason as CallResult /
// GroupSummary: the FastAPI route types its return as `dict[str, Any]`, so
// the oapi-codegen generator emits `*map[string]interface{}` with no typed
// model worth using. Rows stay `[]json.RawMessage` so the renderer pretty-
// prints each row without imposing a per-vendor row shape.
type ResultQueryResult struct {
	HandleID     string            `json:"handle_id"`
	Rows         []json.RawMessage `json:"rows"`
	Offset       int               `json:"offset"`
	Limit        int               `json:"limit"`
	ReturnedRows int               `json:"returned_rows"`
	TotalRows    int               `json:"total_rows"`
	StoredRows   int               `json:"stored_rows"`
	Truncated    bool              `json:"truncated"`
}

// newResultQueryCmd returns the `meho operation result-query` command —
// the CLI verb the vcf-logs result-handle hint already advertises
// (cli/internal/cmd/vcf-logs/query.go), made truthful by #3179.
//
// CLI shape:
//
//	meho operation result-query <handle_id> \
//	  [--offset N]                             # zero-based first row (default 0)
//	  [--limit N]                              # page size (default 50, max 500)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// It wraps POST /api/v1/operations/result-query, the REST twin of the MCP
// `result_query` tool: after `meho operation call` reduces a large list
// response, the reduced envelope carries a `handle` (`result.handle.
// handle_id`); this verb pages the FULL set back beyond the inline sample.
//
// Exit codes mirror `meho operation groups`:
//   - 0   window returned cleanly (including the past-the-end empty window)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. a 404 handle-not-found miss, which
//     carries the backplane's structured `reason=handle_not_found` detail)
func newResultQueryCmd() *cobra.Command {
	var (
		offset            int
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "result-query <handle_id>",
		Short: "Page rows back from a JSONFlux result handle",
		Long: "result-query calls POST /api/v1/operations/result-query — the " +
			"REST twin of the MCP `result_query` tool. After `meho operation " +
			"call` reduces a large list response, the reduced envelope carries " +
			"a `handle` (`result.handle.handle_id`); this verb pages the FULL " +
			"set back beyond the inline sample.\n\n" +
			"--offset advances the window (page by adding the previous --limit); " +
			"--limit sets the page size (default 50, max 500). A window whose " +
			"offset is past the stored row count returns an empty `rows` list — " +
			"that's the end of the set, not an error.\n\n" +
			"A handle that does not exist, has expired (TTL elapsed), or belongs " +
			"to a different operator is a 404 carrying a structured " +
			"`reason=handle_not_found` detail: re-run the original operation to " +
			"mint a fresh handle.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runResultQuery(cmd, resultQueryOptions{
				HandleID:          args[0],
				Offset:            offset,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&offset, "offset", 0,
		"zero-based index of the first row to return (page by advancing this by --limit)")
	cmd.Flags().IntVar(&limit, "limit", 50,
		"page size; default 50, max 500 (matches the result_query MCP tool)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full result-query envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type resultQueryOptions struct {
	HandleID          string
	Offset            int
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

func runResultQuery(cmd *cobra.Command, opts resultQueryOptions) error {
	// Parse the handle UUID CLI-side so a malformed value surfaces locally
	// rather than as a 422 round-trip; the generated body requires
	// `openapi_types.UUID`. Same idiom as `meho scheduler cancel`.
	var handleID openapi_types.UUID
	if err := handleID.UnmarshalText([]byte(opts.HandleID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("handle_id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result, err := postResultQuery(cmd.Context(), client, handleID, opts.Offset, opts.Limit)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printResultQueryResult(cmd.OutOrStdout(), result)
	return nil
}

// postResultQuery issues the typed POST via the generated client, runs the
// one-shot 401-refresh dance via the AuthedClient's Refresh hook (mirroring
// postCall), and unmarshals the 200 body into ResultQueryResult. Non-2xx
// outcomes — including the 404 handle-not-found miss — wrap as
// *apiResponseError for renderRequestError to classify.
func postResultQuery(
	ctx context.Context,
	client operationsAPI,
	handleID openapi_types.UUID,
	offset int,
	limit int,
) (*ResultQueryResult, error) {
	body := api.ResultQueryBody{
		HandleId: handleID,
		Offset:   &offset,
		Limit:    &limit,
	}
	apiParams := &api.PostResultQueryApiV1OperationsResultQueryPostParams{}
	resp, err := client.PostResultQueryApiV1OperationsResultQueryPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.PostResultQueryApiV1OperationsResultQueryPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out ResultQueryResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode result-query response: %w", err)
	}
	return &out, nil
}

// printResultQueryResult renders a ResultQueryResult as a human-readable
// header line plus the pretty-printed rows window. The header states the
// window position + the full/stored counts so the operator knows how far
// they've paged and whether the tail was truncated at spill time; --json
// carries the raw envelope.
func printResultQueryResult(w io.Writer, r *ResultQueryResult) {
	end := r.Offset + r.ReturnedRows
	fmt.Fprintf(w, "%s — rows %d..%d of %d (stored %d), returned %d\n",
		r.HandleID, r.Offset, end, r.TotalRows, r.StoredRows, r.ReturnedRows)
	if r.Truncated {
		fmt.Fprintf(w, "  note: spill was capped at %d of %d rows; rows past %d are not retrievable\n",
			r.StoredRows, r.TotalRows, r.StoredRows)
	}
	if r.ReturnedRows == 0 {
		fmt.Fprintln(w, "  (empty window — offset is at or past the end of the stored set)")
		return
	}
	for _, row := range r.Rows {
		pretty, err := prettyJSON(row)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(row))
		}
	}
}
