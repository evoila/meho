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

// newTier1Cmd returns the `meho nsx tier1` parent command (list sub-verb).
func newTier1Cmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tier1",
		Short:        "NSX tier-1 gateway verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newTier1ListCmd())
	return cmd
}

// newTier1ListCmd returns `meho nsx tier1 list` → nsx.tier1.list.
func newTier1ListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List NSX tier-1 (per-tenant) gateways",
		Long: "list dispatches nsx.tier1.list against\n" +
			"connector_id=\"nsx-rest-4.2\". Renders id / display_name / tier0_path\n" +
			"for human eyes; --json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx tier1 list --target rdc-nsx\n" +
			"  meho nsx tier1 list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTier1List(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runTier1List(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	const opID = "nsx.tier1.list"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printTier1List)
}

func printTier1List(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s nsx.tier1.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 tier-1 gateways)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-50s\n", "id", "display_name", "tier0_path")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-50s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxStringField(e, "tier0_path"), 50),
		)
	}
}
