// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newQueryCmd returns `meho vcf-logs query <constraints>` → the typed
// op vrli.event.query.
//
// The headline read surface of vRLI: event-query against a
// constraints expression. The constraints value is vRLI's
// slash-delimited encoding of field/operator/value tuples plus any
// time-range constraint (e.g. `text/CONTAINS error/timestamp/GT <ms>`);
// the CLI passes the operator-supplied expression through verbatim as
// `params.constraints` and the typed handler renders it into the
// /api/v2/events/<constraints> path via build_event_query_path.
//
// --limit caps the result-set size and is sent as the integer
// `params.limit` the typed op's closed parameter_schema requires
// (the schema accepts only constraints + limit; a time window is
// composed into the constraints chain, not a separate param).
func newQueryCmd() *cobra.Command {
	var (
		targetName        string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "query [constraints]",
		Short: "Run a vRLI event query (constraints, optional limit)",
		Long: "query dispatches the typed op vrli.event.query against connector_id=\n" +
			"\"vrli-rest-9.0\". The constraints positional argument carries vRLI's\n" +
			"slash-delimited constraint expression (e.g. text/CONTAINS+error/timestamp/GT+12345);\n" +
			"empty constraints (no positional argument) is allowed and the appliance returns\n" +
			"the unconstrained set bounded by --limit. Compose any time-range constraint\n" +
			"directly into the constraints expression (the typed op's parameter_schema\n" +
			"accepts only constraints + limit).\n\n" +
			"--limit caps the result set and is sent as the integer params.limit the typed\n" +
			"op requires.\n\n" +
			"Result sets are JSONFlux-handle-shaped (typically large); the human-rendered\n" +
			"output prints a summary plus the handle id when the dispatcher reduces the\n" +
			"payload into a ResultHandle. --json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-logs query --target rdc-vrli\n" +
			"  meho vcf-logs query \"text/CONTAINS+error\" --target rdc-vrli --limit 100\n" +
			"  meho vcf-logs query --target rdc-vrli --json | jq .result",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			constraints := ""
			if len(args) == 1 {
				constraints = args[0]
			}
			return runQuery(cmd, constraints, targetName, limit, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().IntVar(&limit, "limit", 0, "max events to return (0 = appliance default)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runQuery(
	cmd *cobra.Command,
	constraints, targetName string,
	limit int,
	jsonOut bool,
	backplaneOverride string,
) error {
	if limit < 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 0; got %d", limit)),
			jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params := map[string]any{}
	// The typed op vrli.event.query owns a closed parameter_schema
	// (constraints + limit, additionalProperties:false). constraints is
	// rendered into the request path by the handler's
	// build_event_query_path; an empty string reaches the base
	// /api/v2/events/ (all events). limit is sent as an integer — the
	// schema types it as integer, so a string would fail validation.
	params["constraints"] = constraints
	if limit > 0 {
		params["limit"] = limit
	}
	const opID = "vrli.event.query"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printQuery)
}

func printQuery(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s vrli.event.query — status=%s (%.0fms)\n",
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
