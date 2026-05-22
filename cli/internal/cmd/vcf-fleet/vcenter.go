// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

const vcenterListOpID = "GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters"

// newVcenterCmd returns the `meho vcf-fleet vcenter` sub-tree.
func newVcenterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "vcenter",
		Short:        "VCF Fleet vCenter operations (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newVcenterListCmd())
	return cmd
}

func newVcenterListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list <datacenter-vmid>",
		Short: "List vCenters registered under a Fleet datacenter",
		Long: "list dispatches GET:/lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters\n" +
			"against connector_id=\"fleet-rest-9.0\". Requires a datacenter vmid\n" +
			"obtained from `vcf-fleet datacenter list`. The hostname field is the\n" +
			"load-bearing identifier for cross-referencing against vSphere targets.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-fleet vcenter list dc-vmid-001 --target rdc-fleet",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runVcenterList(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runVcenterList(cmd *cobra.Command, datacenterVmid, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"dataCenterVmid": datacenterVmid}
	r, err := dispatchOp(cmd.Context(), backplaneURL, vcenterListOpID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, vcenterListOpID, r, jsonOut, printVcenterList)
}

func printVcenterList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, vcenterListOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		printGenericResult(w, vcenterListOpID, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 vCenters)")
		return
	}
	fmt.Fprintf(w, "%-38s %-40s %-12s %s\n", "vmid", "hostname", "version", "build")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-40s %-12s %s\n",
			truncate(fleetStringField(e, "vmid"), 38),
			truncate(fleetStringField(e, "hostname"), 40),
			truncate(fleetStringField(e, "version"), 12),
			truncate(fleetStringField(e, "buildNumber"), 16),
		)
	}
}
