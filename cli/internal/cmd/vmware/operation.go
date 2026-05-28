// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// newOperationCmd returns the `meho vmware operation` parent command
// and assembles its two pre-scoped meta-tool wrappers (search /
// call). The wrappers re-route to the same /api/v1/operations/*
// routes the generic `meho operation` verbs use but pre-bake the
// connector_id="vmware-rest-9.0" argument so operators don't type
// it on every invocation.
func newOperationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "operation",
		Short:        "Pre-scoped meta-tool wrappers (search / call) for vmware-rest-9.0",
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

// newOperationSearchCmd returns `meho vmware operation search`.
//
// CLI shape:
//
//	meho vmware operation search "<query>" \
//	  [--group <key>]                          # narrow within one group
//	  [--limit N]                              # 1..50, default 10
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
func newOperationSearchCmd() *cobra.Command {
	var (
		groupKey          string
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "search <query>",
		Short: "Hybrid BM25 + cosine RRF search across vmware-rest-9.0 operations",
		Long: "search wraps GET /api/v1/operations/search with connector_id=\n" +
			"\"vmware-rest-9.0\" pre-baked. The query is a free-form prose\n" +
			"intent (e.g. \"list VMs on cluster\", \"snapshot revert\"); the\n" +
			"backplane runs hybrid BM25 + cosine retrieval with Reciprocal\n" +
			"Rank Fusion and returns the top --limit hits (default 10,\n" +
			"clamped at 50 by the API).\n\n" +
			"--group narrows to one group_key within the connector (useful\n" +
			"when the same intent maps to multiple groups — e.g. \"power\"\n" +
			"matches vm.power and host.power).\n\n" +
			"Exit codes mirror meho operation search.",
		Example: "  meho vmware operation search \"list VMs\"\n" +
			"  meho vmware operation search \"snapshot revert\" --group vm.snapshot\n" +
			"  meho vmware operation search \"host evacuate\" --limit 5 --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationSearch(cmd, args[0], groupKey, limit, jsonOut, backplaneOverride)
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
	fmt.Fprintf(w, "%-40s %6s  %s\n", "op_id", "score", "summary")
	for _, h := range r.Hits {
		fmt.Fprintf(w, "%-40s %6.3f  %s\n",
			truncate(h.OpID, 40),
			h.FusedScore,
			truncate(strDeref(h.Summary), 80),
		)
	}
}

// strDeref returns *s or empty string when s is nil. Mirrors the
// operation sibling's helper (duplicated for import-cycle reasons).
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// newOperationCallCmd returns `meho vmware operation call`. Pre-
// scoped wrapper around `meho operation call <connector_id> <op_id>`
// that saves the `vmware-rest-9.0` typing.
//
// CLI shape:
//
//	meho vmware operation call <op_id> \
//	  [--target <slug>]                        # vCenter target slug
//	  [--params '<json>' | @<file>]            # operation params
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
func newOperationCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "call <op_id>",
		Short: "Dispatch any vmware-rest-9.0 op_id (escape hatch for ops without aliases)",
		Long: "call wraps POST /api/v1/operations/call with connector_id=\n" +
			"\"vmware-rest-9.0\" pre-baked. Use this verb when an op doesn't\n" +
			"have a dedicated alias yet (e.g. `meho vmware operation call\n" +
			"DELETE:/api/vcenter/vm/{vm} --target rdc-vcenter --params\n" +
			"'{\"vm\":\"vm-101\"}'`).\n\n" +
			"--target names the vCenter target slug; --params accepts inline\n" +
			"JSON or @<file>; --json emits the OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call:\n" +
			"  - 0   status == ok\n" +
			"  - 1   status == error / denied\n" +
			"  - 2   auth_expired\n" +
			"  - 3   unreachable\n" +
			"  - 4   unexpected response shape",
		Example: "  meho vmware operation call GET:/vcenter/cluster --target rdc-vcenter\n" +
			"  meho vmware operation call DELETE:/api/vcenter/vm/{vm} --target rdc-vcenter --params '{\"vm\":\"vm-101\"}'\n" +
			"  meho vmware operation call vmware.composite.vm.create --target rdc-vcenter --params @new-vm.json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOperationCall(cmd, args[0], targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required for ops that read a target)")
	cmd.Flags().StringVar(&paramsFlag, "params", "",
		"operation params as inline JSON or @<file>; omitted means no params")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
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
