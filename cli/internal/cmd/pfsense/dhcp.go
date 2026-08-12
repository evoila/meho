// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newDhcpCmd returns the `meho pfsense dhcp` parent with one sub-verb:
// `leases` (pfsense.dhcp.leases).
func newDhcpCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "dhcp",
		Short:        "pfSense DHCP sub-verbs (leases)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newDhcpLeasesCmd())
	return cmd
}

// newDhcpLeasesCmd returns the `meho pfsense dhcp leases` command.
//
// Maps to op_id `pfsense.dhcp.leases`. Reads the ISC dhcpd lease
// database (`/var/dhcpd/var/db/dhcpd.leases`) over SSH and returns the
// live DHCPv4 lease table as structured rows.
func newDhcpLeasesCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "leases",
		Short: "List live pfSense DHCPv4 leases (ISC dhcpd lease DB)",
		Long: "leases dispatches pfsense.dhcp.leases and renders the live\n" +
			"DHCPv4 lease table (IP / MAC / hostname / binding state / ends).\n" +
			"Use to see which IPs are leased and gauge DHCP pool exhaustion\n" +
			"before provisioning more hosts on a segment. Only leases in the\n" +
			"'active' binding state are currently live.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense dhcp leases --target pfsense-hetzner-dc\n" +
			"  meho pfsense dhcp leases --target pfsense-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runDhcpLeases(cmd, targetName, jsonOut, backplaneOverride)
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

func runDhcpLeases(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.dhcp.leases", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.dhcp.leases", r, jsonOut, printDhcpLeases)
}

func printDhcpLeases(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.dhcp.leases — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-16s %-18s %-18s %-9s %s\n",
		"IP", "MAC", "HOSTNAME", "STATE", "ENDS (UTC)")
	for _, row := range rows {
		ip := stringField(row, "ip")
		mac := stringField(row, "mac")
		hostname := truncate(stringField(row, "hostname"), 18)
		state := stringField(row, "binding_state")
		ends := stringField(row, "ends")
		fmt.Fprintf(w, "  %-16s %-18s %-18s %-9s %s\n",
			ip, mac, hostname, state, ends)
	}
	fmt.Fprintf(w, "  (%d leases)\n", len(rows))
}
