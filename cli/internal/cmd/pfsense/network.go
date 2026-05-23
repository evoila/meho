// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newNetworkCmd returns the `meho pfsense network` parent with two
// sub-verbs: `interface` (pfsense.interface.list) and `gateway`
// (pfsense.gateway.list).
func newNetworkCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "network",
		Short:        "pfSense network sub-verbs (interface, gateway)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNetworkInterfaceCmd())
	cmd.AddCommand(newNetworkGatewayCmd())
	return cmd
}

// newNetworkInterfaceCmd returns the `meho pfsense network interface` command.
//
// Maps to op_id `pfsense.interface.list`. Runs `ifconfig -a` over SSH
// and returns parsed interface rows.
func newNetworkInterfaceCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "interface",
		Short: "List pfSense network interfaces (ifconfig -a)",
		Long: "interface dispatches pfsense.interface.list and renders the\n" +
			"parsed interface rows (name / MTU / IPv4 / IPv6 / MAC / status).\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense network interface --target pfsense-hetzner-dc\n" +
			"  meho pfsense network interface --target pfsense-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNetworkInterface(cmd, targetName, jsonOut, backplaneOverride)
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

func runNetworkInterface(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.interface.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.interface.list", r, jsonOut, printNetworkInterface)
}

func printNetworkInterface(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.interface.list — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-12s %-8s %-22s %-18s %s\n",
		"INTERFACE", "STATUS", "IPv4", "MAC", "MTU")
	for _, row := range rows {
		name := stringField(row, "name")
		status := stringField(row, "status")
		mac := stringField(row, "ether")
		// inet is a list — join with comma for the one-line render.
		var ipv4 string
		if inet, ok := row["inet"].([]any); ok {
			parts := make([]string, 0, len(inet))
			for _, ip := range inet {
				if s, ok := ip.(string); ok {
					parts = append(parts, s)
				}
			}
			ipv4 = strings.Join(parts, ",")
		}
		mtu := ""
		if m, ok := row["mtu"].(float64); ok {
			mtu = fmt.Sprintf("%d", int(m))
		}
		fmt.Fprintf(w, "  %-12s %-8s %-22s %-18s %s\n",
			name, status, truncate(ipv4, 22), mac, mtu)
	}
	fmt.Fprintf(w, "  (%d interfaces)\n", len(rows))
}

// newNetworkGatewayCmd returns the `meho pfsense network gateway` command.
//
// Maps to op_id `pfsense.gateway.list`. Reads `config.xml` over SSH
// and returns parsed gateway rows.
func newNetworkGatewayCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "gateway",
		Short: "List pfSense routing gateways (from config.xml)",
		Long: "gateway dispatches pfsense.gateway.list and renders the parsed\n" +
			"gateway rows (name / interface / gateway IP / default / descr).\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense network gateway --target pfsense-hetzner-dc\n" +
			"  meho pfsense network gateway --target pfsense-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNetworkGateway(cmd, targetName, jsonOut, backplaneOverride)
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

func runNetworkGateway(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.gateway.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.gateway.list", r, jsonOut, printNetworkGateway)
}

func printNetworkGateway(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.gateway.list — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-20s %-10s %-18s %-7s %s\n",
		"NAME", "IFACE", "GATEWAY", "DEFAULT", "DESCR")
	for _, row := range rows {
		name := stringField(row, "name")
		iface := stringField(row, "interface")
		gw := stringField(row, "gateway")
		descr := truncate(stringField(row, "descr"), 30)
		isDefault := "no"
		if d, ok := row["defaultgw"].(bool); ok && d {
			isDefault = "YES"
		}
		fmt.Fprintf(w, "  %-20s %-10s %-18s %-7s %s\n",
			name, iface, gw, isDefault, descr)
	}
	fmt.Fprintf(w, "  (%d gateways)\n", len(rows))
}
