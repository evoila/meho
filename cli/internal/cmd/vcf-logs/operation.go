// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcflogs

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// newOperationCmd returns `meho vcf-logs operation` with search / call
// meta-tool wrappers pre-scoped to vrli-rest-9.0.
func newOperationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "Pre-scoped meta-tool wrappers (search / call) for vrli-rest-9.0",
		SilenceUsage: true,
	}
	cmd.AddCommand(newOperationSearchCmd())
	cmd.AddCommand(newOperationCallCmd())
	return cmd
}

// searchHit + searchResponse alias the dispatch-package types
// promoted from this dir's pre-#1274 copies. The verb file references
// the unqualified names so the per-verb pretty-printers + tests stay
// readable; the underlying types live once in dispatch.
type (
	searchHit      = dispatch.SearchHit
	searchResponse = dispatch.SearchResponse
)

func newOperationSearchCmd() *cobra.Command {
	var (
		groupKey          string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Hybrid BM25 + cosine RRF search across vrli-rest-9.0 operations",
		Long: "search wraps GET /api/v1/operations/search with connector_id=\n" +
			"\"vrli-rest-9.0\" pre-baked.\n\n" +
			"Exit codes mirror meho operation search.",
		Example: "  meho vcf-logs operation search \"event query\"\n" +
			"  meho vcf-logs operation search \"alerts\" --group vrli-alerts",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationSearch(cmd, args[0], groupKey, limit, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&groupKey, "group", "", "narrow the search to one group_key")
	cmd.Flags().IntVar(&limit, "limit", 10, "max hits (1..50, clamped by the API)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit machine-readable JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOperationSearch(cmd *cobra.Command, query, groupKey string, limit int, jsonOut bool, backplaneOverride string) error {
	if limit < 1 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 1; got %d", limit)),
			jsonOut)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	result, err := conn.Search(cmd.Context(), backplaneURL, query, groupKey, limit)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printSearchTable(cmd.OutOrStdout(), query, result)
	return nil
}

func printSearchTable(w io.Writer, query string, r *searchResponse) {
	fmt.Fprintf(w, "search %s %q — %d hit(s) in %.0fms\n",
		ConnectorID, query, len(r.Hits), r.QueryDurationMs)
	if len(r.Hits) == 0 {
		return
	}
	fmt.Fprintf(w, "%-50s %6s  %s\n", "op_id", "score", "summary")
	for _, h := range r.Hits {
		fmt.Fprintf(w, "%-50s %6.3f  %s\n",
			truncate(h.OpID, 50),
			h.FusedScore,
			truncate(strDeref(h.Summary), 80),
		)
	}
}

func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func newOperationCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "call <op_id>",
		Short: "Dispatch any vrli-rest-9.0 op_id (escape hatch for ops without aliases)",
		Long: "call wraps POST /api/v1/operations/call with connector_id=\n" +
			"\"vrli-rest-9.0\" pre-baked. Use when an op doesn't have a\n" +
			"dedicated alias yet.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-logs operation call GET:/api/v2/version --target rdc-vrli\n" +
			"  meho vcf-logs operation call GET:/api/v2/events/{constraints} --target rdc-vrli --params '{\"constraints\":\"\"}'",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationCall(cmd, args[0], targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vRLI target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runOperationCall(cmd *cobra.Command, opID, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, nil)
}
