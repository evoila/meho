// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newSupermetricCmd returns the `meho vcf-operations supermetric`
// parent command (list sub-verb).
func newSupermetricCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "supermetric",
		Short:        "vROps super-metric verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSupermetricListCmd())
	return cmd
}

// newSupermetricListCmd returns `meho vcf-operations supermetric list` →
// GET:/suite-api/api/supermetrics.
//
// --params is the escape hatch for filter query parameters
// (“id“ (repeatable) / “name“ / “page“ / “pageSize“).
func newSupermetricListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps super metrics (user-defined metric formulae)",
		Long: "list dispatches GET:/suite-api/api/supermetrics against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders id / name / formula (first line);\n" +
			"--json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(id (repeatable) / name / page / pageSize).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations supermetric list --target rdc-vrops\n" +
			"  meho vcf-operations supermetric list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runSupermetricList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runSupermetricList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/supermetrics"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printSupermetricList)
}

func printSupermetricList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/supermetrics — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/supermetrics"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 super metrics)")
		return
	}
	fmt.Fprintf(w, "%-38s %-40s %-50s\n", "id", "name", "formula")
	for _, e := range entries {
		formulaFirstLine := strings.SplitN(vropsStringField(e, "formula"), "\n", 2)[0]
		fmt.Fprintf(w, "%-38s %-40s %-50s\n",
			truncate(vropsStringField(e, "id"), 38),
			truncate(vropsStringField(e, "name"), 40),
			truncate(formulaFirstLine, 50),
		)
	}
}
