// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newRouteCmd returns the `meho pfsense route` parent with one sub-verb:
// `list` (pfsense.route.static.list).
func newRouteCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "route",
		Short:        "pfSense static-route sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRouteListCmd())
	return cmd
}

// newRouteListCmd returns the `meho pfsense route list` command.
//
// Maps to op_id `pfsense.route.static.list`. Reads `config.xml` over SSH
// and returns one row per static route (network, gateway, descr,
// disabled).
func newRouteListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List pfSense static routes (from config.xml)",
		Long: "list dispatches pfsense.route.static.list and shows the static\n" +
			"routes (network / gateway / disabled / descr). The NETWORK value\n" +
			"is what pfsense.route.static.delete takes to delete a route.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense route list --target fw-01\n" +
			"  meho pfsense route list --target fw-01 --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRouteList(cmd, targetName, jsonOut, backplaneOverride)
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

func runRouteList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.route.static.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.route.static.list", r, jsonOut, printRouteList)
}

func printRouteList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.route.static.list — status=%s (%.0fms)\n",
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
	fmt.Fprintf(w, "  %-20s %-20s %-8s %s\n",
		"NETWORK", "GATEWAY", "DISABLED", "DESCR")
	for _, row := range rows {
		network := stringField(row, "network")
		gateway := stringField(row, "gateway")
		disabled := "no"
		if d, ok := row["disabled"].(bool); ok && d {
			disabled = "YES"
		}
		descr := truncate(stringField(row, "descr"), 30)
		fmt.Fprintf(w, "  %-20s %-20s %-8s %s\n",
			network, gateway, disabled, descr)
	}
	fmt.Fprintf(w, "  (%d routes)\n", len(rows))
}
