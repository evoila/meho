// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newFirewallCmd returns the `meho pfsense firewall` parent with two
// sub-verbs: `rules` (pfsense.firewall.rules) and `state`
// (pfsense.firewall.state).
func newFirewallCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "firewall",
		Short:        "pfSense firewall sub-verbs (rules, state)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newFirewallRulesCmd())
	cmd.AddCommand(newFirewallStateCmd())
	return cmd
}

// newFirewallRulesCmd returns the `meho pfsense firewall rules` command.
//
// Maps to op_id `pfsense.firewall.rules`. Runs `pfctl -sr` over SSH
// and returns the active filter rules as structured rows.
func newFirewallRulesCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "rules",
		Short: "List active pfSense firewall filter rules (pfctl -sr)",
		Long: "rules dispatches pfsense.firewall.rules and renders the active\n" +
			"filter ruleset as a table of action / direction / rule rows.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense firewall rules --target pfsense-hetzner-dc\n" +
			"  meho pfsense firewall rules --target pfsense-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runFirewallRules(cmd, targetName, jsonOut, backplaneOverride)
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

func runFirewallRules(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.firewall.rules", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.firewall.rules", r, jsonOut, printFirewallRules)
}

func printFirewallRules(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.firewall.rules — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-8s %-10s  %s\n", "ACTION", "DIRECTION", "RULE")
	for _, row := range rows {
		action := stringField(row, "action")
		if action == "" {
			action = "?"
		}
		dir := stringField(row, "direction")
		if dir == "" {
			dir = ""
		}
		rule := truncate(stringField(row, "rule"), 80)
		fmt.Fprintf(w, "  %-8s %-10s  %s\n", action, dir, rule)
	}
	fmt.Fprintf(w, "  (%d rules)\n", len(rows))
}

// newFirewallStateCmd returns the `meho pfsense firewall state` command.
//
// Maps to op_id `pfsense.firewall.state`. Runs `pfctl -ss` over SSH
// and returns the active connection-state table as structured rows.
// The state table can be large on busy firewalls; --json is preferred
// for piping into jq on busy systems.
func newFirewallStateCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "state",
		Short: "List active pfSense connection-state table entries (pfctl -ss)",
		Long: "state dispatches pfsense.firewall.state and renders the active\n" +
			"connection-state table as proto / iface / src / direction /\n" +
			"dst rows. On busy firewalls the table can contain thousands of\n" +
			"rows — use --json and pipe through `jq` for filtering.\n\n" +
			"When the JSONFlux reducer is configured and the row count\n" +
			"exceeds the threshold, the result includes a handle for\n" +
			"paging via result_describe / result_query.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense firewall state --target pfsense-hetzner-dc\n" +
			"  meho pfsense firewall state --target pfsense-hetzner-dc --json | jq '.result.rows | length'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runFirewallState(cmd, targetName, jsonOut, backplaneOverride)
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

func runFirewallState(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.firewall.state", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.firewall.state", r, jsonOut, printFirewallState)
}

func printFirewallState(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.firewall.state — status=%s (%.0fms)\n",
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
	// Limit human output to first 20 rows to avoid terminal flood.
	// --json is the path for full inspection.
	limit := len(rows)
	truncated := false
	if limit > 20 {
		limit = 20
		truncated = true
	}
	fmt.Fprintf(w, "  %-6s %-8s %-22s %-5s %-22s\n",
		"PROTO", "IFACE", "SRC", "DIR", "DST")
	for _, row := range rows[:limit] {
		proto := stringField(row, "proto")
		iface := stringField(row, "iface")
		src := truncate(stringField(row, "src"), 22)
		dir := stringField(row, "direction")
		dst := truncate(stringField(row, "dst"), 22)
		fmt.Fprintf(w, "  %-6s %-8s %-22s %-5s %-22s\n",
			proto, iface, src, dir, dst)
	}
	if truncated {
		fmt.Fprintf(w, "  … (%d more rows — use --json to inspect all)\n", len(rows)-20)
	}
	fmt.Fprintf(w, "  (%d total states)\n", len(rows))
}
