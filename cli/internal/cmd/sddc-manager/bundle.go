// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newBundleCmd returns the `meho sddc-manager bundle` sub-tree.
func newBundleCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "bundle",
		Short:        "VCF LCM bundle operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newBundleListCmd())
	return cmd
}

func newBundleListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List LCM bundles (VCF update packages, async patches)",
		Long: "list dispatches GET:/v1/bundles against connector_id=\"sddc-rest-9.0\".\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager bundle list --target rdc-sddc-manager\n" +
			"  meho sddc-manager bundle list --target rdc-sddc-manager --json | jq '.result.elements[].version'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runBundleList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runBundleList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "GET:/v1/bundles", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "GET:/v1/bundles", r, jsonOut, printBundleList)
}

func printBundleList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		conn.PrintGeneric(w, "GET:/v1/bundles", r)
		return
	}
	fmt.Fprintf(w, "VCF LCM bundles (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 bundles)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-16s  %-12s  %-10s  %s\n", "id", "version", "compliant", "applicable", "description")
	for _, e := range entries {
		compliant := "?"
		if v, ok := e["isCompliant"]; ok {
			if b, ok := v.(bool); ok {
				if b {
					compliant = "yes"
				} else {
					compliant = "no"
				}
			}
		}
		applicable := truncate(sddcStringField(e, "applicabilityStatus"), 10)
		fmt.Fprintf(w, "%-36s  %-16s  %-12s  %-10s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			truncate(sddcStringField(e, "version"), 16),
			compliant,
			applicable,
			truncate(sddcStringField(e, "description"), 60),
		)
	}
}
