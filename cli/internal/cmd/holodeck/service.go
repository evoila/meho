// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newServiceCmd returns the `meho holodeck service` parent with one
// sub-verb: `list` (holodeck.service.list).
func newServiceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "service",
		Short:        "Holodeck Photon service sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newServiceListCmd())
	return cmd
}

// newServiceListCmd returns the `meho holodeck service list` command.
//
// Maps to op_id `holodeck.service.list`. Runs `Get-Service |
// Where-Object { $_.Name -like 'Holo*' } | Select-Object
// Name,Status,DisplayName | ConvertTo-Json -Depth 4` over the
// pwsh-over-SSH transport and returns a JSONFlux-shaped
// `{rows, total}` envelope.
func newServiceListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Holodeck Photon services and their status",
		Long: "list dispatches holodeck.service.list and renders the bundled\n" +
			"Holodeck Photon services (DHCP, DNS, NTP, FRR-BGP, Webtop,\n" +
			"K8s-in-appliance) with their Status. Pair with\n" +
			"`meho holodeck logs tail <component>` for drill-in.\n\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck service list --target holorouter-hetzner-dc\n" +
			"  meho holodeck service list --target holorouter-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runServiceList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runServiceList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.service.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.service.list", r, jsonOut, printServiceList)
}

func printServiceList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.service.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-20s %-12s %s\n", "NAME", "STATUS", "DISPLAY-NAME")
	for _, row := range rows {
		name := stringField(row, "Name")
		status := stringField(row, "Status")
		display := truncate(stringField(row, "DisplayName"), 48)
		fmt.Fprintf(w, "  %-20s %-12s %s\n",
			truncate(name, 20), status, display)
	}
	fmt.Fprintf(w, "  (%d services)\n", len(rows))
}
