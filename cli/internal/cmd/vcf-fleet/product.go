// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

const productListOpID = "GET:/lcm/lcops/api/v2/environments/{environmentId}/products"

// newProductCmd returns the `meho vcf-fleet product` sub-tree.
func newProductCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "product",
		Short:        "VCF Fleet product operations (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newProductListCmd())
	return cmd
}

func newProductListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list <environment-id>",
		Short: "List products deployed under a Fleet environment",
		Long: "list dispatches GET:/lcm/lcops/api/v2/environments/{environmentId}/products\n" +
			"against connector_id=\"fleet-rest-9.0\". Returns one entry per\n" +
			"product (vRA, vROps, vRLI, vIDM, Postgres, ...) with deployment\n" +
			"status, version, and node breakdown. Requires an environmentId\n" +
			"from `vcf-fleet environment list`.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-fleet product list env-vrops-prod --target rdc-fleet",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runProductList(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runProductList(cmd *cobra.Command, environmentID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"environmentId": environmentID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, productListOpID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, productListOpID, r, jsonOut, printProductList)
}

func printProductList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, productListOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		printGenericResult(w, productListOpID, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 products)")
		return
	}
	fmt.Fprintf(w, "%-16s %-20s %s\n", "productId", "version", "status")
	for _, e := range entries {
		fmt.Fprintf(w, "%-16s %-20s %s\n",
			truncate(fleetStringField(e, "productId"), 16),
			truncate(fleetStringField(e, "version"), 20),
			truncate(fleetStringField(e, "status"), 32),
		)
	}
}
