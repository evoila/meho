// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newNatCmd returns the `meho pfsense nat` parent with one sub-verb:
// `rules` (pfsense.nat.rules).
func newNatCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "nat",
		Short:        "pfSense NAT sub-verbs (rules)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newNatRulesCmd())
	return cmd
}

// newNatRulesCmd returns the `meho pfsense nat rules` command.
//
// Maps to op_id `pfsense.nat.rules`. Runs `pfctl -sn` over SSH and
// returns the active NAT ruleset as structured rows.
func newNatRulesCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "rules",
		Short: "List active pfSense NAT ruleset (pfctl -sn)",
		Long: "rules dispatches pfsense.nat.rules and renders the active NAT\n" +
			"ruleset as a table of action / direction / rule rows.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense nat rules --target pfsense-hetzner-dc\n" +
			"  meho pfsense nat rules --target pfsense-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runNatRules(cmd, targetName, jsonOut, backplaneOverride)
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

func runNatRules(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.nat.rules", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.nat.rules", r, jsonOut, printNatRules)
}

func printNatRules(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.nat.rules — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-10s %-10s  %s\n", "ACTION", "DIRECTION", "RULE")
	for _, row := range rows {
		action := stringField(row, "action")
		if action == "" {
			action = "?"
		}
		dir := stringField(row, "direction")
		rule := truncate(stringField(row, "rule"), 80)
		fmt.Fprintf(w, "  %-10s %-10s  %s\n", action, dir, rule)
	}
	fmt.Fprintf(w, "  (%d NAT rules)\n", len(rows))
}
