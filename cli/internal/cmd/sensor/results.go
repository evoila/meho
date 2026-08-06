// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// validResultStates mirrors the backend CheckState vocabulary the trend
// query's --state filter accepts.
var validResultStates = map[string]bool{
	"ok": true, "degraded": true, "critical": true, "unknown": true, "skip": true,
}

// newResultsCmd returns the `meho sensor results` command.
//
//	meho sensor results <sensor_id> [--from T] [--to T] [--state S]
//	                    [--limit N] [--cursor C] [--json] [--backplane <url>]
//
// Role: operator. The forensic per-tick evidence trend query (#2756):
// binary filters only, deterministic evaluated_at ASC order, raw rows.
func newResultsCmd() *cobra.Command {
	var (
		from              string
		to                string
		state             string
		limit             int
		cursor            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "results <sensor_id>",
		Short: "Show a sensor's per-tick evidence history (trend query)",
		Long: "results calls GET /api/v1/sensors/{id}/results and renders one " +
			"sensor's per-tick evidence history (#2756), oldest-first " +
			"(evaluated_at ASC) — the forensic 'when did this start degrading / " +
			"how fast is it filling' view the latest-result projection discards. " +
			"Role: operator, scoped to your own tenant; a cross-tenant / absent " +
			"id returns 404 sensor_not_found.\n\n" +
			"Binary filters only: --from / --to bound an inclusive evaluated_at " +
			"window (RFC 3339, e.g. 2026-08-01T00:00:00Z); --state narrows to an " +
			"exact ok|degraded|critical|unknown|skip; --limit caps the page " +
			"(1..500, server default 100). There are no smoothing / downsampling " +
			"/ aggregation knobs — the client aggregates the raw rows.\n\n" +
			"Pages via an opaque keyset --cursor: when a page is full the output " +
			"prints the next cursor; paste it back with --cursor to continue. " +
			"--json emits the raw {items, next_cursor} envelope.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runResults(cmd, resultsOptions{
				SensorID:          args[0],
				From:              from,
				To:                to,
				State:             state,
				Limit:             limit,
				Cursor:            cursor,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&from, "from", "",
		"inclusive lower bound on evaluated_at (RFC 3339, e.g. 2026-08-01T00:00:00Z)")
	cmd.Flags().StringVar(&to, "to", "",
		"inclusive upper bound on evaluated_at (RFC 3339, e.g. 2026-08-02T00:00:00Z)")
	cmd.Flags().StringVar(&state, "state", "",
		"filter by state: ok | degraded | critical | unknown | skip")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows per page (1..500, server default 100 when omitted)")
	cmd.Flags().StringVar(&cursor, "cursor", "",
		"opaque keyset pagination token (echo the printed next cursor to continue)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw {items, next_cursor} JSON envelope instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type resultsOptions struct {
	SensorID          string
	From              string
	To                string
	State             string
	Limit             int
	Cursor            string
	JSONOut           bool
	BackplaneOverride string
}

func runResults(cmd *cobra.Command, opts resultsOptions) error {
	if opts.SensorID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("results requires a non-empty <sensor_id> argument"), opts.JSONOut)
	}
	var sensorID openapi_types.UUID
	if err := sensorID.UnmarshalText([]byte(opts.SensorID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("sensor-id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	if opts.State != "" && !validResultStates[opts.State] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--state must be one of: ok, degraded, critical, unknown, skip"),
			opts.JSONOut)
	}
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut)
	}
	from, err := parseRFC3339Flag(opts.From, "--from")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	to, err := parseRFC3339Flag(opts.To, "--to")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := getResults(cmd.Context(), backplaneURL, sensorID, opts, from, to)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil (the
	// generated parser only populates it when Content-Type is JSON). Without
	// the guard a malformed 200 would print "no evidence rows" as if the
	// window were genuinely empty — actively misleading.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a results payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printResultsTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// parseRFC3339Flag parses an optional RFC 3339 timestamp flag; an empty
// value yields a nil pointer so the filter is omitted.
func parseRFC3339Flag(raw, flagName string) (*time.Time, error) {
	if raw == "" {
		return nil, nil
	}
	parsed, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return nil, fmt.Errorf("%s is not a valid RFC 3339 timestamp: %v", flagName, err)
	}
	return &parsed, nil
}

// resultsQueryParams maps the CLI flags onto the generated query-param shape.
// --state is a typed enum pointer; --from / --to are time pointers; --limit /
// --cursor only send when set so the backend defaults apply when omitted.
func resultsQueryParams(
	opts resultsOptions,
	from, to *time.Time,
) *api.ListSensorResultsApiV1SensorsSensorIdResultsGetParams {
	params := &api.ListSensorResultsApiV1SensorsSensorIdResultsGetParams{}
	if from != nil {
		params.From = from
	}
	if to != nil {
		params.To = to
	}
	if opts.State != "" {
		s := api.ListSensorResultsApiV1SensorsSensorIdResultsGetParamsState(opts.State)
		params.State = &s
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

// getResults calls GET /api/v1/sensors/{id}/results via the generated typed
// client.
func getResults(
	ctx context.Context,
	backplaneURL string,
	sensorID openapi_types.UUID,
	opts resultsOptions,
	from, to *time.Time,
) (*api.ListSensorResultsApiV1SensorsSensorIdResultsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := resultsQueryParams(opts, from, to)
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ListSensorResultsApiV1SensorsSensorIdResultsGetResponse, error) {
			return authed.ListSensorResultsApiV1SensorsSensorIdResultsGetWithResponse(ctx, sensorID, params)
		},
		func(r *api.ListSensorResultsApiV1SensorsSensorIdResultsGetResponse) int { return r.StatusCode() },
	)
}

// printResultsTable renders the evidence rows in the server's returned order
// (evaluated_at ASC — do NOT re-sort): EVALUATED_AT, STATE, VALUE, REASON. A
// full page surfaces the next keyset cursor as a paste-to-continue hint.
func printResultsTable(w io.Writer, r *api.SensorResultListResponse) {
	if r == nil || len(r.Items) == 0 {
		fmt.Fprintln(w, "no evidence rows matched the filter")
		return
	}
	fmt.Fprintf(w, "%-30s %-10s %-24s %s\n", "EVALUATED_AT", "STATE", "VALUE", "REASON")
	for _, row := range r.Items {
		evaluatedAt := row.EvaluatedAt
		reason := "-"
		if row.Reason != nil && *row.Reason != "" {
			reason = sanitizeCell(*row.Reason)
		}
		fmt.Fprintf(w, "%-30s %-10s %-24s %s\n",
			formatTime(&evaluatedAt), string(row.State),
			sanitizeCell(formatResultValue(row.Value)), reason)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}

// formatResultValue renders the observed JSON scalar for the table. A nil
// value (an unknown outcome) shows as "-"; everything else is its default
// Go rendering, which is compact for the scalar types the value carries
// (bool / number / string).
func formatResultValue(v any) string {
	if v == nil {
		return "-"
	}
	return fmt.Sprintf("%v", v)
}
