// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAggregatedCmd returns `meho vcf-logs aggregated <constraints>` →
// GET:/api/v2/aggregated-events/{constraints}.
//
// Group-by aggregation over the same constraint shape `query`
// accepts. The bin / group_key / aggregation-function (count, sum,
// avg, min, max) are encoded in the constraints string the same
// way vRLI's URL-segment grammar specifies. --time-range threads
// to `params.timestamp_window` for the time bucket bound.
func newAggregatedCmd() *cobra.Command {
	var (
		targetName        string
		timeRange         string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "aggregated [constraints]",
		Short: "Run a vRLI aggregated event query (group-by / count / time-bin)",
		Long: "aggregated dispatches GET:/api/v2/aggregated-events/{constraints} against\n" +
			"connector_id=\"vrli-rest-9.0\". Group-by aggregation over the same constraint\n" +
			"shape vcf-logs.query accepts, plus a bin-by / group-by clause and an aggregation\n" +
			"function (count, sum, avg, min, max).\n\n" +
			"--time-range threads to params.timestamp_window the same way `query` consumes it.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-logs aggregated --target rdc-vrli --time-range 24h\n" +
			"  meho vcf-logs aggregated \"text/CONTAINS+error+bin-width=3600\" --target rdc-vrli\n",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			constraints := ""
			if len(args) == 1 {
				constraints = args[0]
			}
			return runAggregated(cmd, constraints, targetName, timeRange, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().StringVar(&timeRange, "time-range", "",
		"aggregation time window (e.g. 5m, 1h, 24h, 7d); empty = appliance default")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAggregated(
	cmd *cobra.Command,
	constraints, targetName, timeRange string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"constraints": constraints}
	if timeRange != "" {
		params["timestamp_window"] = timeRange
	}
	const opID = "GET:/api/v2/aggregated-events/{constraints}"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printAggregated)
}

func printAggregated(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v2/aggregated-events/{constraints} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var agg struct {
		Bins []vrliEntry `json:"bins"`
	}
	if err := jsonUnmarshalStrict(r.Result, &agg); err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(agg.Bins) == 0 {
		fmt.Fprintln(w, "  (0 bins)")
		return
	}
	fmt.Fprintf(w, "%-30s %s\n", "bucket", "value")
	for _, b := range agg.Bins {
		key := vrliStringField(b, "groupKey")
		if key == "" {
			key = vrliStringField(b, "minTimestamp")
		}
		var val any = "?"
		if v, ok := b["value"]; ok {
			val = v
		}
		fmt.Fprintf(w, "%-30s %v\n", truncate(key, 30), val)
	}
}
