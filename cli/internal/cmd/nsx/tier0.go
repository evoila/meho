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

// newTier0Cmd returns the `meho nsx tier0` parent command (list sub-verb).
func newTier0Cmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "tier0",
		Short:        "NSX tier-0 gateway verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newTier0ListCmd())
	return cmd
}

// newTier0ListCmd returns `meho nsx tier0 list` → GET:/policy/api/v1/infra/tier-0s.
func newTier0ListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List NSX tier-0 (provider edge) gateways",
		Long: "list dispatches GET:/policy/api/v1/infra/tier-0s against\n" +
			"connector_id=\"nsx-rest-4.2\". Renders id / display_name / ha_mode\n" +
			"for human eyes; --json emits the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx tier0 list --target rdc-nsx\n" +
			"  meho nsx tier0 list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTier0List(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runTier0List(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	const opID = "GET:/policy/api/v1/infra/tier-0s"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printTier0List)
}

func printTier0List(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/policy/api/v1/infra/tier-0s — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 tier-0 gateways)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-16s\n", "id", "display_name", "ha_mode")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-16s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxStringField(e, "ha_mode"), 16),
		)
	}
}
