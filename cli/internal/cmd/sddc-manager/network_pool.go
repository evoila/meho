// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sddcmanager

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newNetworkPoolCmd returns the `meho sddc-manager network-pool` sub-tree.
func newNetworkPoolCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "network-pool",
		Short:        "VCF network pool operations (list / get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNetworkPoolListCmd())
	cmd.AddCommand(newNetworkPoolGetCmd())
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
		Long: "list dispatches sddc.network_pool.list against connector_id=\"sddc-rest-9.0\".\n\n" +
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
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, "sddc.network_pool.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "sddc.network_pool.list", r, jsonOut, printNetworkPoolList)
}

func printNetworkPoolList(w io.Writer, r *CallResult) {
	entries, err := decodeElementsResult(r.Result)
	if err != nil || r.Status != "ok" {
		conn.PrintGeneric(w, "sddc.network_pool.list", r)
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

func newNetworkPoolGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <network-pool-id>",
		Short: "Show one network pool's networks with free/used IP capacity",
		Long: "get dispatches sddc.network_pool.get against connector_id=\"sddc-rest-9.0\".\n" +
			"Requires a pool id from `sddc-manager network-pool list`. This is the\n" +
			"host-commissioning IP-capacity pre-flight: free/used = free vs used IP\n" +
			"counts per VMOTION/vSAN network.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho sddc-manager network-pool get np-01 --target rdc-sddc-manager",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runNetworkPoolGet(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "SDDC Manager target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runNetworkPoolGet(cmd *cobra.Command, poolID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params := map[string]any{"id": poolID}
	r, err := conn.Call(cmd.Context(), backplaneURL, "sddc.network_pool.get", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, "sddc.network_pool.get", r, jsonOut, printNetworkPoolGet)
}

func printNetworkPoolGet(w io.Writer, r *CallResult) {
	if r.Status != "ok" {
		conn.PrintGeneric(w, "sddc.network_pool.get", r)
		return
	}
	var p struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Networks []struct {
			Type    string `json:"type"`
			VlanID  int    `json:"vlanId"`
			Subnet  string `json:"subnet"`
			Mask    string `json:"mask"`
			Gateway string `json:"gateway"`
			IPPools []struct {
				Start string `json:"start"`
				End   string `json:"end"`
			} `json:"ipPools"`
			FreeIps []string `json:"freeIps"`
			UsedIps []string `json:"usedIps"`
		} `json:"networks"`
	}
	if err := jsonUnmarshalStrict(r.Result, &p); err != nil || p.ID == "" {
		conn.PrintGeneric(w, "sddc.network_pool.get", r)
		return
	}
	fmt.Fprintf(w, "network pool: %s (%s) — %d network(s)\n", p.Name, p.ID, len(p.Networks))
	for _, n := range p.Networks {
		fmt.Fprintf(w, "  %-9s vlan=%-5d subnet=%s/%s gw=%s  free=%d used=%d\n",
			n.Type, n.VlanID, n.Subnet, n.Mask, n.Gateway, len(n.FreeIps), len(n.UsedIps))
		for _, pool := range n.IPPools {
			fmt.Fprintf(w, "      ip-pool: %s - %s\n", pool.Start, pool.End)
		}
	}
}
