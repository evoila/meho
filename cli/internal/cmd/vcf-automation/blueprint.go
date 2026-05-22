// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"
)

// Tenant-plane verb: `meho vcf-automation blueprint list` -- templates
// deployments instantiate.
func newBlueprintCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "blueprint",
		Short:        "Tenant-plane VCFA catalog blueprints (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBlueprintListCmd())
	return cmd
}

func newBlueprintListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List tenant-plane catalog blueprints",
		Example:       "  meho vcf-automation blueprint list --target rdc-vcfa",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTenantListVerb(cmd,
				"GET:/iaas/api/blueprints",
				targetName, jsonOut, backplaneOverride,
				printBlueprintList,
			)
		},
	}
	addStandardFlags(cmd, &targetName, &backplaneOverride, &jsonOut)
	return cmd
}

func printBlueprintList(w io.Writer, r *CallResult) {
	const opID = "GET:/iaas/api/blueprints"
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeTenantListResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 blueprints)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-10s\n", "id", "name", "status", "version")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-30s  %-12s  %-10s\n",
			truncate(vcfaStringField(e, "id"), 36),
			truncate(vcfaStringField(e, "name"), 30),
			truncate(vcfaStringField(e, "status"), 12),
			truncate(vcfaStringField(e, "version"), 10),
		)
	}
}
