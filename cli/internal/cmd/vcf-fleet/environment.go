// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcffleet

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

const (
	environmentListOpID = "GET:/lcm/lcops/api/v2/environments"
	environmentGetOpID  = "GET:/lcm/lcops/api/v2/environments/{environmentId}"
)

// newEnvironmentCmd returns the `meho vcf-fleet environment` sub-tree.
func newEnvironmentCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "environment",
		Short:        "VCF Fleet environment operations (list / info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newEnvironmentListCmd())
	cmd.AddCommand(newEnvironmentInfoCmd())
	return cmd
}

func newEnvironmentListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Fleet-managed environments (the primary inventory unit)",
		Long: "list dispatches GET:/lcm/lcops/api/v2/environments against\n" +
			"connector_id=\"fleet-rest-9.0\". Each environment groups one or more\n" +
			"product deployments. Large appliances may return a JSONFlux handle\n" +
			"through the shared HandleStore — use result_describe / result_query\n" +
			"to navigate the full set when present.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-fleet environment list --target rdc-fleet",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEnvironmentList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runEnvironmentList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, environmentListOpID, targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, environmentListOpID, r, jsonOut, printEnvironmentList)
}

func printEnvironmentList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, environmentListOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil {
		conn.PrintGeneric(w, environmentListOpID, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 environments)")
		return
	}
	fmt.Fprintf(w, "%-38s %-32s %s\n", "environmentId", "environmentName", "environmentStatus")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-32s %s\n",
			truncate(fleetStringField(e, "environmentId"), 38),
			truncate(fleetStringField(e, "environmentName"), 32),
			truncate(fleetStringField(e, "environmentStatus"), 32),
		)
	}
}

func newEnvironmentInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <environment-id>",
		Short: "Show the full detail of one Fleet environment",
		Long: "info dispatches GET:/lcm/lcops/api/v2/environments/{environmentId}\n" +
			"against connector_id=\"fleet-rest-9.0\". Returns products[] with full\n" +
			"deployment metadata (nodes, IPs, versions, FQDN), configuration\n" +
			"history, and status transitions.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       "  meho vcf-fleet environment info env-vrops-prod --target rdc-fleet",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEnvironmentInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "VCF Fleet target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runEnvironmentInfo(cmd *cobra.Command, environmentID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"environmentId": environmentID}
	r, err := conn.Call(cmd.Context(), backplaneURL, environmentGetOpID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, environmentGetOpID, r, jsonOut, printEnvironmentInfo)
}

func printEnvironmentInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, environmentGetOpID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var env struct {
		EnvironmentID     string `json:"environmentId"`
		EnvironmentName   string `json:"environmentName"`
		EnvironmentStatus string `json:"environmentStatus"`
		TransactionID     string `json:"transactionId"`
		CreatedOn         string `json:"createdOn"`
		Products          []struct {
			ProductID string `json:"productId"`
			Version   string `json:"version"`
			Status    string `json:"status"`
			Nodes     []struct {
				Hostname  string `json:"hostname"`
				IPAddress string `json:"ipAddress"`
				Role      string `json:"role"`
				VMStatus  string `json:"vmStatus"`
			} `json:"nodes"`
		} `json:"products"`
	}
	if err := jsonUnmarshalStrict(r.Result, &env); err != nil || env.EnvironmentID == "" {
		if pretty, perr := dispatch.PrettyJSON(r.Result); perr == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	fmt.Fprintf(w, "environment:  %s (%s) — status=%s\n", env.EnvironmentName, env.EnvironmentID, env.EnvironmentStatus)
	if env.TransactionID != "" {
		fmt.Fprintf(w, "transaction:  %s\n", env.TransactionID)
	}
	if env.CreatedOn != "" {
		fmt.Fprintf(w, "created_on:   %s\n", env.CreatedOn)
	}
	if len(env.Products) > 0 {
		fmt.Fprintln(w, "products:")
		for _, p := range env.Products {
			fmt.Fprintf(w, "  %-12s  version=%-16s  status=%s\n", p.ProductID, p.Version, p.Status)
			for _, n := range p.Nodes {
				fmt.Fprintf(w, "    %-30s  %-16s  role=%-10s  vm=%s\n",
					n.Hostname, n.IPAddress, n.Role, n.VMStatus)
			}
		}
	}
}
