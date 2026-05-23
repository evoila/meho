// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

const datacenterListOpID = "GET:/lcm/lcops/api/v2/datacenters"

// newDatacenterCmd returns the `meho vcf-fleet datacenter` sub-tree.
func newDatacenterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "datacenter",
		Short:        "VCF Fleet datacenter operations (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newDatacenterListCmd())
	return cmd
}

func newDatacenterListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Fleet-managed datacenters (wrapper-verified reachability probe)",
		Long: "list dispatches GET:/lcm/lcops/api/v2/datacenters against\n" +
			"connector_id=\"fleet-rest-9.0\". This is the wrapper-verified\n" +
			"reachability probe — guaranteed to respond in VCF 9.0 even when\n" +
			"/about returns HTTP 500. The vmid in each entry is the\n" +
			"load-bearing identifier for `vcf-fleet vcenter list <vmid>`.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho vcf-fleet datacenter list --target rdc-fleet\n" +
			"  meho vcf-fleet datacenter list --target rdc-fleet --json | jq '.result[].vmid'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runDatacenterList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runDatacenterList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, datacenterListOpID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, datacenterListOpID, r, jsonOut, printDatacenterList)
}

func printDatacenterList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, datacenterListOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		conn.PrintGeneric(w, datacenterListOpID, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 datacenters)")
		return
	}
	fmt.Fprintf(w, "%-38s %-32s %-16s %s\n", "vmid", "name", "type", "city")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-32s %-16s %s\n",
			truncate(fleetStringField(e, "vmid"), 38),
			truncate(fleetStringField(e, "name"), 32),
			truncate(fleetStringField(e, "type"), 16),
			truncate(fleetStringField(e, "city"), 32),
		)
	}
}
