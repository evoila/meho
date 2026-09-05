// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// ResultQueryResult mirrors the backend result-query envelope — the
// `dict[str, Any]` returned by POST /api/v1/operations/result-query
// (read_result_window / run_result_query). Hand-written for the same reason
// as CallResult / GroupSummary: the FastAPI route types its return as
// `dict[str, Any]`, so the oapi-codegen generator emits
// `*map[string]interface{}` with no typed model worth using. Rows stay
// `[]json.RawMessage` so the renderer pretty-prints each row without imposing
// a per-vendor row shape. Coverage / CoverageNote are populated only in query
// mode (#3366).
type ResultQueryResult struct {
	HandleID     string            `json:"handle_id"`
	Rows         []json.RawMessage `json:"rows"`
	Offset       int               `json:"offset"`
	Limit        int               `json:"limit"`
	ReturnedRows int               `json:"returned_rows"`
	TotalRows    int               `json:"total_rows"`
	StoredRows   int               `json:"stored_rows"`
	Truncated    bool              `json:"truncated"`
	Coverage     string            `json:"coverage,omitempty"`
	CoverageNote *string           `json:"coverage_note,omitempty"`
}

// newResultQueryCmd returns the `meho operation result-query` command —
// the CLI verb the vcf-logs result-handle hint already advertises
// (cli/internal/cmd/vcf-logs/query.go), made truthful by #3179 and given a
// bounded query surface by #3366.
//
// CLI shape:
//
//	meho operation result-query <handle_id> \
//	  [--offset N]                             # paging: zero-based first row (default 0)
//	  [--limit N]                              # paging: page size (default 50, max 500)
//	  [--where "<field> <op> <value>"]         # query: repeatable WHERE predicate
//	  [--select <field>]                       # query: repeatable projection column
//	  [--group-by <field>]                     # query: repeatable GROUP BY key
//	  [--aggregate "<FUNC> [field]"]           # query: repeatable aggregate
//	  [--order-by "<field> [asc|desc]"]        # query: repeatable sort term
//	  [--query-limit N]                        # query: max output rows (clamps to 500)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// It wraps POST /api/v1/operations/result-query, the REST twin of the MCP
// `result_query` tool. With no query flags it pages the FULL set back beyond
// the inline sample; with any query flag it runs one bounded, read-only query
// server-side (filter / project / group / aggregate) and `--offset` /
// `--limit` are ignored.
//
// Exit codes mirror `meho operation groups`:
//   - 0   window/result returned cleanly (including the past-the-end empty window)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. a 404 handle-not-found miss, which
//     carries the backplane's structured `reason=handle_not_found` detail)
func newResultQueryCmd() *cobra.Command {
	var (
		offset            int
		limit             int
		whereTerms        []string
		selectCols        []string
		groupByCols       []string
		aggregateTerms    []string
		orderByTerms      []string
		queryLimit        int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "result-query <handle_id>",
		Short: "Page or query rows back from a JSONFlux result handle",
		Long: "result-query calls POST /api/v1/operations/result-query — the " +
			"REST twin of the MCP `result_query` tool. After `meho operation " +
			"call` reduces a large list response, the reduced envelope carries " +
			"a `handle` (`result.handle.handle_id`); this verb reads the FULL " +
			"set back beyond the inline sample.\n\n" +
			"Paging mode (default): --offset advances the window (page by adding " +
			"the previous --limit); --limit sets the page size (default 50, max " +
			"500). A window whose offset is past the stored row count returns an " +
			"empty `rows` list — that's the end of the set, not an error.\n\n" +
			"Query mode: pass any of --where / --select / --group-by / " +
			"--aggregate / --order-by / --query-limit to run one bounded, " +
			"read-only query server-side instead of paging. --where takes " +
			"\"<field> <op> [value]\" (op one of = != < <= > >= IN 'IS NULL'; " +
			"IN takes a comma-separated value list); --aggregate takes " +
			"\"<FUNC> [field]\" (FUNC one of COUNT SUM MIN MAX AVG); --order-by " +
			"takes \"<field> [asc|desc]\". Every referenced field must be a " +
			"column on the handle; there is no raw-SQL argument. Results carry a " +
			"`coverage` label — `partial` means the spill was capped so a count " +
			"covers only the stored subset, not the whole inventory.\n\n" +
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
				WhereTerms:        whereTerms,
				SelectCols:        selectCols,
				GroupByCols:       groupByCols,
				AggregateTerms:    aggregateTerms,
				OrderByTerms:      orderByTerms,
				QueryLimit:        queryLimit,
				QueryLimitSet:     cmd.Flags().Changed("query-limit"),
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&offset, "offset", 0,
		"paging: zero-based index of the first row to return (advance by --limit)")
	cmd.Flags().IntVar(&limit, "limit", 50,
		"paging: page size; default 50, max 500 (matches the result_query MCP tool)")
	cmd.Flags().StringArrayVar(&whereTerms, "where", nil,
		"query: WHERE predicate \"<field> <op> [value]\" (repeatable; op = != < <= > >= IN 'IS NULL')")
	cmd.Flags().StringArrayVar(&selectCols, "select", nil,
		"query: projection column to return (repeatable; omit for all columns)")
	cmd.Flags().StringArrayVar(&groupByCols, "group-by", nil,
		"query: GROUP BY key column (repeatable, max 4)")
	cmd.Flags().StringArrayVar(&aggregateTerms, "aggregate", nil,
		"query: aggregate \"<FUNC> [field]\" (repeatable; FUNC = COUNT SUM MIN MAX AVG)")
	cmd.Flags().StringArrayVar(&orderByTerms, "order-by", nil,
		"query: sort term \"<field> [asc|desc]\" (repeatable, max 4)")
	cmd.Flags().IntVar(&queryLimit, "query-limit", 0,
		"query: max output rows (clamps to 500); the result flags truncation when more matched")
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
	WhereTerms        []string
	SelectCols        []string
	GroupByCols       []string
	AggregateTerms    []string
	OrderByTerms      []string
	QueryLimit        int
	QueryLimitSet     bool
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
	spec, err := buildResultQuerySpec(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result, err := postResultQuery(cmd.Context(), client, handleID, opts.Offset, opts.Limit, spec)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printResultQueryResult(cmd.OutOrStdout(), result)
	return nil
}

// buildResultQuerySpec assembles the structured query from the query flags,
// or returns nil when none were passed (paging mode). Field-vs-schema
// validation happens server-side; this only shapes the request.
func buildResultQuerySpec(opts resultQueryOptions) (*api.ResultQuerySpec, error) {
	if len(opts.WhereTerms) == 0 && len(opts.SelectCols) == 0 &&
		len(opts.GroupByCols) == 0 && len(opts.AggregateTerms) == 0 &&
		len(opts.OrderByTerms) == 0 && !opts.QueryLimitSet {
		return nil, nil
	}
	spec := &api.ResultQuerySpec{}
	if len(opts.WhereTerms) > 0 {
		preds := make([]api.FilterPredicate, 0, len(opts.WhereTerms))
		for _, raw := range opts.WhereTerms {
			pred, err := parseWherePredicate(raw)
			if err != nil {
				return nil, err
			}
			preds = append(preds, pred)
		}
		spec.Filter = &preds
	}
	if len(opts.SelectCols) > 0 {
		spec.Select = &opts.SelectCols
	}
	if len(opts.GroupByCols) > 0 {
		spec.GroupBy = &opts.GroupByCols
	}
	if len(opts.AggregateTerms) > 0 {
		aggs := make([]api.Aggregate, 0, len(opts.AggregateTerms))
		for _, raw := range opts.AggregateTerms {
			agg, err := parseAggregate(raw)
			if err != nil {
				return nil, err
			}
			aggs = append(aggs, agg)
		}
		spec.Aggregate = &aggs
	}
	if len(opts.OrderByTerms) > 0 {
		terms := make([]api.OrderBy, 0, len(opts.OrderByTerms))
		for _, raw := range opts.OrderByTerms {
			term, err := parseOrderBy(raw)
			if err != nil {
				return nil, err
			}
			terms = append(terms, term)
		}
		spec.OrderBy = &terms
	}
	if opts.QueryLimitSet {
		limit := opts.QueryLimit
		spec.Limit = &limit
	}
	return spec, nil
}

// parseWherePredicate parses "<field> <op> [value]" into a FilterPredicate.
// The value is JSON-typed where possible (so `id = 5` binds an integer, not
// the string "5"); IN takes a comma-separated list; IS NULL takes no value.
func parseWherePredicate(raw string) (api.FilterPredicate, error) {
	s := strings.TrimSpace(raw)
	if lower := strings.ToLower(s); strings.HasSuffix(lower, "is null") {
		field := strings.TrimSpace(s[:len(s)-len("is null")])
		if field == "" {
			return api.FilterPredicate{}, fmt.Errorf("--where %q: missing field before IS NULL", raw)
		}
		return api.FilterPredicate{Field: field, Op: api.FilterPredicateOpISNULL}, nil
	}
	parts := strings.Fields(s)
	if len(parts) < 3 {
		return api.FilterPredicate{}, fmt.Errorf(
			"--where %q: expected \"<field> <op> <value>\" (op one of = != < <= > >= IN 'IS NULL')", raw)
	}
	field := parts[0]
	opToken := parts[1]
	valueText := strings.TrimSpace(s[strings.Index(s, opToken)+len(opToken):])
	op, ok := normalizeWhereOp(opToken)
	if !ok {
		return api.FilterPredicate{}, fmt.Errorf(
			"--where %q: unsupported operator %q (use = != < <= > >= IN 'IS NULL')", raw, opToken)
	}
	pred := api.FilterPredicate{Field: field, Op: op}
	if op == api.FilterPredicateOpIN {
		items := []any{}
		for _, tok := range strings.Split(valueText, ",") {
			items = append(items, jsonTypedValue(strings.TrimSpace(tok)))
		}
		var v any = items
		pred.Value = v
		return pred, nil
	}
	pred.Value = jsonTypedValue(valueText)
	return pred, nil
}

func normalizeWhereOp(token string) (api.FilterPredicateOp, bool) {
	switch strings.ToUpper(token) {
	case "=":
		return api.FilterPredicateOpEqual, true
	case "!=":
		return api.FilterPredicateOpEmpty, true
	case "<":
		return api.FilterPredicateOpLessThan, true
	case "<=":
		return api.FilterPredicateOpLessThanEqual, true
	case ">":
		return api.FilterPredicateOpGreaterThan, true
	case ">=":
		return api.FilterPredicateOpGreaterThanEqual, true
	case "IN":
		return api.FilterPredicateOpIN, true
	default:
		return "", false
	}
}

// jsonTypedValue returns the JSON-decoded value of s (number, bool, null)
// when it parses as a JSON scalar, else the raw string — so numeric and
// boolean literals bind with their real type while bare words stay strings.
func jsonTypedValue(s string) any {
	var v any
	if err := json.Unmarshal([]byte(s), &v); err == nil {
		switch v.(type) {
		case float64, bool, nil:
			return v
		}
	}
	return s
}

func parseAggregate(raw string) (api.Aggregate, error) {
	parts := strings.Fields(strings.TrimSpace(raw))
	if len(parts) == 0 {
		return api.Aggregate{}, fmt.Errorf("--aggregate %q: expected \"<FUNC> [field]\"", raw)
	}
	fn := api.AggregateFunc(strings.ToUpper(parts[0]))
	switch fn {
	case api.AggregateFuncCOUNT, api.AggregateFuncSUM, api.AggregateFuncMIN,
		api.AggregateFuncMAX, api.AggregateFuncAVG:
	default:
		return api.Aggregate{}, fmt.Errorf(
			"--aggregate %q: unsupported function %q (use COUNT SUM MIN MAX AVG)", raw, parts[0])
	}
	agg := api.Aggregate{Func: fn}
	if len(parts) > 1 {
		field := parts[1]
		agg.Field = &field
	}
	return agg, nil
}

func parseOrderBy(raw string) (api.OrderBy, error) {
	parts := strings.Fields(strings.TrimSpace(raw))
	if len(parts) == 0 {
		return api.OrderBy{}, fmt.Errorf("--order-by %q: expected \"<field> [asc|desc]\"", raw)
	}
	term := api.OrderBy{Field: parts[0]}
	if len(parts) > 1 {
		switch strings.ToLower(parts[1]) {
		case "asc":
			dir := api.OrderByDirectionAsc
			term.Direction = &dir
		case "desc":
			dir := api.OrderByDirectionDesc
			term.Direction = &dir
		default:
			return api.OrderBy{}, fmt.Errorf(
				"--order-by %q: direction must be asc or desc", raw)
		}
	}
	return term, nil
}

// postResultQuery issues the typed POST via the generated client, runs the
// one-shot 401-refresh dance via the AuthedClient's Refresh hook (mirroring
// postCall), and unmarshals the 200 body into ResultQueryResult. When *spec*
// is non-nil the request carries the structured query (query mode) and the
// backend ignores offset/limit. Non-2xx outcomes — including the 404
// handle-not-found miss — wrap as *apiResponseError for renderRequestError.
func postResultQuery(
	ctx context.Context,
	client operationsAPI,
	handleID openapi_types.UUID,
	offset int,
	limit int,
	spec *api.ResultQuerySpec,
) (*ResultQueryResult, error) {
	body := api.ResultQueryBody{
		HandleId: handleID,
		Offset:   &offset,
		Limit:    &limit,
		Query:    spec,
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
// header line plus the pretty-printed rows. The header states the window
// position + the full/stored counts; a query-mode result additionally prints
// its coverage caveat when the spill was capped. --json carries the raw
// envelope.
func printResultQueryResult(w io.Writer, r *ResultQueryResult) {
	end := r.Offset + r.ReturnedRows
	fmt.Fprintf(w, "%s — rows %d..%d of %d (stored %d), returned %d\n",
		r.HandleID, r.Offset, end, r.TotalRows, r.StoredRows, r.ReturnedRows)
	if r.Coverage == "partial" && r.CoverageNote != nil {
		fmt.Fprintf(w, "  coverage: %s\n", *r.CoverageNote)
	}
	if r.Truncated {
		if r.Coverage != "" {
			fmt.Fprintf(w, "  note: output row cap reached (%d rows); more matched — narrow the query or raise --query-limit\n",
				r.ReturnedRows)
		} else {
			fmt.Fprintf(w, "  note: spill was capped at %d of %d rows; rows past %d are not retrievable\n",
				r.StoredRows, r.TotalRows, r.StoredRows)
		}
	}
	if r.ReturnedRows == 0 {
		fmt.Fprintln(w, "  (empty window — no rows matched, or the offset is past the end of the stored set)")
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
