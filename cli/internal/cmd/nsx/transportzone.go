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

const tzOpID = "GET:/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"

// newTransportZoneCmd returns `meho nsx transport-zone` parent command.
func newTransportZoneCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "transport-zone",
		Short:        "NSX transport-zone verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newTransportZoneListCmd())
	return cmd
}

// newTransportZoneListCmd returns `meho nsx transport-zone list`.
func newTransportZoneListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List NSX transport zones under the default enforcement point",
		Long: "list dispatches GET:/policy/api/v1/infra/sites/default/enforcement-points/\n" +
			"default/transport-zones against connector_id=\"nsx-rest-4.2\".\n" +
			"Renders id / display_name / tz_type for human eyes; --json emits\n" +
			"the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho nsx transport-zone list --target rdc-nsx\n" +
			"  meho nsx transport-zone list --target rdc-nsx --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTransportZoneList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "NSX target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runTransportZoneList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, tzOpID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, tzOpID, r, jsonOut, printTransportZoneList)
}

func printTransportZoneList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s transport-zones — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 transport zones)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-10s\n", "id", "display_name", "tz_type")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-10s\n",
			truncate(nsxStringField(e, "id"), 38),
			truncate(nsxStringField(e, "display_name"), 30),
			truncate(nsxStringField(e, "tz_type"), 10),
		)
	}
}
