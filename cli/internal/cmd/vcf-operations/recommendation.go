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

// newRecommendationCmd returns the `meho vcf-operations recommendation`
// parent command (list sub-verb).
func newRecommendationCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "recommendation",
		Short:        "vROps recommendation verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newRecommendationListCmd())
	return cmd
}

// newRecommendationListCmd returns
// `meho vcf-operations recommendation list` →
// GET:/suite-api/api/recommendations.
//
// --params is the escape hatch for filter query parameters
// (“id“ (repeatable) / “page“ / “pageSize“).
func newRecommendationListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps recommendations (remediation hints attached to alerts/symptoms)",
		Long: "list dispatches GET:/suite-api/api/recommendations against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders id / description (first line) /\n" +
			"actionId; --json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(id (repeatable) / page / pageSize).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations recommendation list --target rdc-vrops\n" +
			"  meho vcf-operations recommendation list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runRecommendationList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runRecommendationList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/recommendations"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printRecommendationList)
}

func printRecommendationList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/recommendations — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/recommendations"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 recommendations)")
		return
	}
	fmt.Fprintf(w, "%-38s %-60s %-12s\n", "id", "description", "actionId")
	for _, e := range entries {
		descriptionFirstLine := strings.SplitN(vropsStringField(e, "description"), "\n", 2)[0]
		fmt.Fprintf(w, "%-38s %-60s %-12s\n",
			truncate(vropsStringField(e, "id"), 38),
			truncate(descriptionFirstLine, 60),
			truncate(vropsStringField(e, "actionId"), 12),
		)
	}
}
