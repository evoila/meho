// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newSegmentCmd returns the `meho nsx segment` parent command (list sub-verb).
func newSegmentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "segment",
		Short:        "NSX segment verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSegmentListCmd())
	return cmd
}

// newSegmentListCmd returns `meho nsx segment list` → GET:/policy/api/v1/infra/segments.
func newSegmentListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List NSX policy-API segments (logical + DVS-backed portgroups)",
		Long: "list dispatches GET:/policy/api/v1/infra/segments against\n" +
			"connector_id=\"nsx-rest-4.2\". Renders id / display_name /\n" +
			"transport_zone_path for human eyes; --json emits the full envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx segment list --target rdc-nsx\n" +
			"  meho nsx segment list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSegmentList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSegmentList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	const opID = "GET:/policy/api/v1/infra/segments"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printSegmentList)
}

func printSegmentList(w io.Writer, r *CallResult) {
	const opID = "GET:/policy/api/v1/infra/segments"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeNsxListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 segments)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-30s\n", "id", "display_name", "transport_zone")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-30s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxPathBasename(nsxStringField(e, "transport_zone_path")), 30),
		)
	}
}
