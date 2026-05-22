// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newAlertCmd returns the `meho vcf-operations alert` parent command
// (list sub-verb).
func newAlertCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "alert",
		Short:        "vROps alert verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAlertListCmd())
	return cmd
}

// newAlertListCmd returns `meho vcf-operations alert list` →
// GET:/suite-api/api/alerts.
//
// --params is the escape hatch for filter query parameters
// (“activeOnly“ / “alertCriticality“ / “alertStatus“ /
// “resourceId“ / “page“ / “pageSize“).
func newAlertListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps alerts (currently firing or recently resolved)",
		Long: "list dispatches GET:/suite-api/api/alerts against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders alertId / alertDefinitionName /\n" +
			"resourceId / status; --json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(activeOnly / alertCriticality / alertStatus / resourceId / page /\n" +
			"pageSize). Useful filters:\n" +
			"  --params '{\"activeOnly\":true}'                  - only active alerts\n" +
			"  --params '{\"alertCriticality\":\"CRITICAL\"}'      - severity-scoped\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations alert list --target rdc-vrops\n" +
			"  meho vcf-operations alert list --target rdc-vrops --params '{\"activeOnly\":true}'\n" +
			"  meho vcf-operations alert list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAlertList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAlertList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/alerts"
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printAlertList)
}

func printAlertList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/alerts — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/alerts"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 alerts)")
		return
	}
	fmt.Fprintf(w, "%-38s %-32s %-22s %-10s %-10s\n",
		"alertId", "definition", "resourceId", "level", "status")
	for _, e := range entries {
		level := ""
		if v, ok := e["alertLevel"].(float64); ok {
			level = fmt.Sprintf("%d", int(v))
		}
		fmt.Fprintf(w, "%-38s %-32s %-22s %-10s %-10s\n",
			truncate(vropsStringField(e, "alertId"), 38),
			truncate(vropsStringField(e, "alertDefinitionName"), 32),
			truncate(vropsStringField(e, "resourceId"), 22),
			level,
			truncate(vropsStringField(e, "status"), 10),
		)
	}
}
