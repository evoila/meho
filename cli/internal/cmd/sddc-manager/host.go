// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newHostCmd returns the `meho sddc-manager host` sub-tree.
func newHostCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "host",
		Short:        "VCF ESXi host operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newHostListCmd())
	return cmd
}

func newHostListCmd() *cobra.Command {
	var (
		targetName        string
		domainID          string
		clusterID         string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List ESXi hosts across all or one VCF domain or cluster",
		Long: "list dispatches GET:/v1/hosts against connector_id=\"sddc-rest-9.0\".\n" +
			"Pass --domain or --cluster to filter. Large deployments may return\n" +
			"hundreds of rows — use --json for machine-readable output.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho sddc-manager host list --target rdc-sddc-manager\n" +
			"  meho sddc-manager host list --domain domain-wld01 --target rdc-sddc-manager",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runHostList(cmd, targetName, domainID, clusterID, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().StringVar(&domainID, "domain", "", "filter to a specific domain id")
	cmd.Flags().StringVar(&clusterID, "cluster", "", "filter to a specific cluster id")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runHostList(cmd *cobra.Command, targetName, domainID, clusterID string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	var params map[string]any
	if domainID != "" || clusterID != "" {
		params = map[string]any{}
		if domainID != "" {
			params["domainId"] = domainID
		}
		if clusterID != "" {
			params["clusterId"] = clusterID
		}
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/v1/hosts", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/v1/hosts", r, jsonOut, printHostList)
}

func printHostList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		printGenericResult(w, "GET:/v1/hosts", r)
		return
	}
	fmt.Fprintf(w, "ESXi hosts (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 hosts)")
		return
	}
	fmt.Fprintf(w, "%-36s  %-45s  %-12s  %s\n", "id", "fqdn", "esxi_version", "status")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %-45s  %-12s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			truncate(sddcStringField(e, "fqdn"), 45),
			sddcStringField(e, "esxiVersion"),
			sddcStringField(e, "status"),
		)
	}
}
