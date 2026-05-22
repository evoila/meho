// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newQueryCmd returns `meho vcf-logs query <constraints>` →
// GET:/api/v2/events/{constraints}.
//
// The headline read surface of vRLI: event-query against a
// constraints expression. The `{constraints}` path segment is
// vRLI's URI-segment encoding of field/operator/value tuples
// (e.g. `text/CONTAINS+error/hostname/CONTAINS+vcsa`); the CLI
// passes the operator-supplied expression through verbatim as
// `params.constraints` and lets the dispatcher's
// “_substitute_path“ thread it into the path template.
//
// --time-range honours the Goal #214 G3.6 vRLI DoD line — the flag
// maps to “params.timestamp_window“ which the backend converts
// to the vRLI event-query timestamp constraint (default behaviour
// is the appliance's "all-time" window, matching the wrapper).
// --limit caps the result-set size via the query param.
func newQueryCmd() *cobra.Command {
	var (
		targetName        string
		timeRange         string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "query [constraints]",
		Short: "Run a vRLI event query (constraints, optional time range, optional limit)",
		Long: "query dispatches GET:/api/v2/events/{constraints} against connector_id=\n" +
			"\"vrli-rest-9.0\". The constraints positional argument carries vRLI's\n" +
			"URI-segment-encoded constraint expression (e.g. text/CONTAINS+error+timestamp/GT+12345);\n" +
			"empty constraints (no positional argument) is allowed and the appliance returns\n" +
			"the unconstrained set bounded by --time-range / --limit.\n\n" +
			"--time-range threads to params.timestamp_window — the backend rewrites this into\n" +
			"the vRLI timestamp constraint on the event-query path. --limit caps the result\n" +
			"set via query-string.\n\n" +
			"Result sets are JSONFlux-handle-shaped (typically large); the human-rendered\n" +
			"output prints a summary plus the handle id when the dispatcher reduces the\n" +
			"payload into a ResultHandle. --json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-logs query --target rdc-vrli --time-range 1h\n" +
			"  meho vcf-logs query \"text/CONTAINS+error\" --target rdc-vrli --time-range 24h --limit 100\n" +
			"  meho vcf-logs query --target rdc-vrli --json | jq .result",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			constraints := ""
			if len(args) == 1 {
				constraints = args[0]
			}
			return runQuery(cmd, constraints, targetName, timeRange, limit, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().StringVar(&timeRange, "time-range", "",
		"event-query time window (e.g. 5m, 1h, 24h, 7d); empty = appliance default")
	cmd.Flags().IntVar(&limit, "limit", 0, "max events to return (0 = appliance default)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runQuery(
	cmd *cobra.Command,
	constraints, targetName, timeRange string,
	limit int,
	jsonOut bool,
	backplaneOverride string,
) error {
	if limit < 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 0; got %d", limit)),
			jsonOut)
	}
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{}
	// Constraints is path-template substitution; the dispatcher's
	// _substitute_path consumes the empty-string when no positional was
	// supplied (vRLI treats an empty trailing segment as no extra
	// constraint).
	params["constraints"] = constraints
	if timeRange != "" {
		params["timestamp_window"] = timeRange
	}
	if limit > 0 {
		params["limit"] = strconv.Itoa(limit)
	}
	const opID = "GET:/api/v2/events/{constraints}"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printQuery)
}

func printQuery(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/events/{constraints} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	// vRLI returns {"events": [...], "complete": bool} when the
	// reducer leaves the payload as-is; the JSONFlux reducer
	// summarises to {"row_count": N, "handle": ...} when the
	// handle path fires (Initiative #369 DoD: at least one list
	// op per connector exercises the handle path; events is the
	// canonical candidate). Handle the reducer-summarised shape
	// first; fall back to the raw envelope for the pass-through
	// case.
	var asHandle struct {
		RowCount int `json:"row_count"`
	}
	if err := jsonUnmarshalStrict(r.Result, &asHandle); err == nil && asHandle.RowCount > 0 {
		fmt.Fprintf(w, "  rows: %d (result-handle path — use `meho operation result-query` to drill in)\n",
			asHandle.RowCount)
		return
	}
	var events struct {
		Events   []vrliEntry `json:"events"`
		Complete *bool       `json:"complete"`
	}
	if err := jsonUnmarshalStrict(r.Result, &events); err != nil {
		fallbackResultRender(w, r)
		return
	}
	count := len(events.Events)
	complete := "?"
	if events.Complete != nil {
		if *events.Complete {
			complete = "true"
		} else {
			complete = "false"
		}
	}
	fmt.Fprintf(w, "  events:   %d\n", count)
	fmt.Fprintf(w, "  complete: %s\n", complete)
	if count == 0 {
		return
	}
	fmt.Fprintf(w, "%-26s %-24s %s\n", "timestamp", "hostname", "text")
	for _, e := range events.Events {
		ts := vrliStringField(e, "timestamp")
		host := vrliStringField(e, "hostname")
		text := vrliStringField(e, "text")
		fmt.Fprintf(w, "%-26s %-24s %s\n",
			truncate(ts, 26),
			truncate(host, 24),
			truncate(text, 96),
		)
	}
}
