// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newClusterCmd returns the `meho sddc-manager cluster` sub-tree.
func newClusterCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "cluster",
		Short:        "VCF cluster operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newClusterListCmd())
	return cmd
}

func newClusterListCmd() *cobra.Command {
	var (
		targetName        string
		domainID          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vSphere clusters across all or one VCF domain",
		Long: "list dispatches GET:/v1/clusters against connector_id=\"sddc-rest-9.0\".\n" +
			"Pass --domain to filter to a specific domain.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager cluster list --target rdc-sddc-manager\n" +
			"  meho sddc-manager cluster list --domain domain-mgmt --target rdc-sddc-manager",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runClusterList(cmd, targetName, domainID, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().StringVar(&domainID, "domain", "", "filter to a specific domain id")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runClusterList(cmd *cobra.Command, targetName, domainID string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	var params map[string]any
	if domainID != "" {
		params = map[string]any{"domainId": domainID}
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/v1/clusters", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/v1/clusters", r, jsonOut, printClusterList)
}

func printClusterList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		printGenericResult(w, "GET:/v1/clusters", r)
		return
	}
	fmt.Fprintf(w, "VCF clusters (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 clusters)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-32s  %-16s  %s\n", "id", "name", "datastore_type", "domain_id")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-32s  %-16s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			truncate(sddcStringField(e, "name"), 32),
			sddcStringField(e, "primaryDatastoreType"),
			sddcStringField(e, "domainId"),
		)
	}
}
