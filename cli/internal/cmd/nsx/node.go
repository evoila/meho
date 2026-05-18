// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newNodeCmd returns the `meho nsx node` parent command (list sub-verb).
func newNodeCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "node",
		Short:        "NSX transport-node verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNodeListCmd())
	return cmd
}

// newNodeListCmd returns `meho nsx node list` → GET:/api/v1/transport-nodes.
func newNodeListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List NSX transport nodes (ESXi + edge)",
		Long: "list dispatches GET:/api/v1/transport-nodes against connector_id=\n" +
			"\"nsx-rest-4.2\". Renders id / display_name / type for human eyes;\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx node list --target rdc-nsx\n" +
			"  meho nsx node list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNodeList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runNodeList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/api/v1/transport-nodes", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/api/v1/transport-nodes", r, jsonOut, printNodeList)
}

func printNodeList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/api/v1/transport-nodes — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 transport nodes)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-12s\n", "id", "display_name", "type")
	for _, e := range entries {
		nodeType := ""
		if info, ok := e["node_deployment_info"].(map[string]any); ok {
			nodeType = truncate(nsxStringField(info, "resource_type"), 12)
		}
		fmt.Fprintf(w, "%-38s %-30s %-12s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			nodeType,
		)
	}
}
