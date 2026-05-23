// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newAlertDefinitionCmd returns the `meho vcf-operations alertdefinition`
// parent command (list sub-verb).
func newAlertDefinitionCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "alertdefinition",
		Short:        "vROps alert-definition verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAlertDefinitionListCmd())
	return cmd
}

// newAlertDefinitionListCmd returns
// `meho vcf-operations alertdefinition list` →
// GET:/suite-api/api/alertdefinitions.
//
// --params is the escape hatch for filter query parameters
// (“id“ (repeatable) / “adapterKind“ / “resourceKind“ /
// “name“ / “page“ / “pageSize“).
func newAlertDefinitionListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps alert definitions (the policy surface)",
		Long: "list dispatches GET:/suite-api/api/alertdefinitions against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders id / name / adapterKindKey /\n" +
			"resourceKindKey; --json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(id (repeatable) / adapterKind / resourceKind / name / page /\n" +
			"pageSize).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations alertdefinition list --target rdc-vrops\n" +
			"  meho vcf-operations alertdefinition list --target rdc-vrops " +
			"--params '{\"adapterKind\":\"VMWARE\",\"resourceKind\":\"VirtualMachine\"}'\n" +
			"  meho vcf-operations alertdefinition list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runAlertDefinitionList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runAlertDefinitionList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/alertdefinitions"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printAlertDefinitionList)
}

func printAlertDefinitionList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/alertdefinitions — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/alertdefinitions"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 alert definitions)")
		return
	}
	fmt.Fprintf(w, "%-46s %-30s %-16s %-22s\n", "id", "name", "adapterKind", "resourceKind")
	for _, e := range entries {
		fmt.Fprintf(w, "%-46s %-30s %-16s %-22s\n",
			truncate(vropsStringField(e, "id"), 46),
			truncate(vropsStringField(e, "name"), 30),
			truncate(vropsStringField(e, "adapterKindKey"), 16),
			truncate(vropsStringField(e, "resourceKindKey"), 22),
		)
	}
}
