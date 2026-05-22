// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newSymptomCmd returns the `meho vcf-operations symptom` parent
// command (list sub-verb).
func newSymptomCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "symptom",
		Short:        "vROps symptom verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSymptomListCmd())
	return cmd
}

// newSymptomListCmd returns `meho vcf-operations symptom list` →
// GET:/suite-api/api/symptoms.
//
// --params is the escape hatch for filter query parameters
// (“id“ (repeatable) / “resourceId“ / “activeOnly“ /
// “statusType“ / “page“ / “pageSize“).
func newSymptomListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps symptoms (per-condition signals beneath alerts)",
		Long: "list dispatches GET:/suite-api/api/symptoms against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders id / symptomDefinitionName /\n" +
			"resourceId / severity; --json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(id (repeatable) / resourceId / activeOnly / statusType / page /\n" +
			"pageSize).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations symptom list --target rdc-vrops\n" +
			"  meho vcf-operations symptom list --target rdc-vrops --params '{\"activeOnly\":true}'\n" +
			"  meho vcf-operations symptom list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSymptomList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSymptomList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/symptoms"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printSymptomList)
}

func printSymptomList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/symptoms — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/symptoms"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 symptoms)")
		return
	}
	fmt.Fprintf(w, "%-38s %-32s %-22s %-12s\n",
		"id", "definition", "resourceId", "severity")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-32s %-22s %-12s\n",
			truncate(vropsStringField(e, "id"), 38),
			truncate(vropsStringField(e, "symptomDefinitionName"), 32),
			truncate(vropsStringField(e, "resourceId"), 22),
			truncate(vropsStringField(e, "severity"), 12),
		)
	}
}
