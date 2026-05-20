// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newNetworkPoolCmd returns the `meho sddc-manager network-pool` sub-tree.
func newNetworkPoolCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "network-pool",
		Short:        "VCF network pool operations",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNetworkPoolListCmd())
	return cmd
}

func newNetworkPoolListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List VCF network pools (IP ranges and VLANs for host commission)",
		Long: "list dispatches GET:/v1/network-pools against connector_id=\"sddc-rest-9.0\".\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho sddc-manager network-pool list --target rdc-sddc-manager",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNetworkPoolList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runNetworkPoolList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/v1/network-pools", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "GET:/v1/network-pools", r, jsonOut, printNetworkPoolList)
}

func printNetworkPoolList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		printGenericResult(w, "GET:/v1/network-pools", r)
		return
	}
	fmt.Fprintf(w, "VCF network pools (%d)\n", len(entries))
	if len(entries) == 0 {
		fmt.Fprintln(w, "(0 network pools)")
		return
	}
	fmt.Fprintf(w, "%-36s  %s\n", "id", "name")
	for _, e := range entries {
		fmt.Fprintf(w, "%-36s  %s\n",
			truncate(sddcStringField(e, "id"), 36),
			sddcStringField(e, "name"),
		)
	}
}
