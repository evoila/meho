// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newConfigCmd returns the `meho holodeck config` parent with one
// sub-verb: `show` (holodeck.config.show).
func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "config",
		Short:        "Holodeck appliance config sub-verbs (show)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newConfigShowCmd())
	return cmd
}

// newConfigShowCmd returns the `meho holodeck config show` command.
//
// Maps to op_id `holodeck.config.show`. Runs `Get-HoloDeckConfig |
// ConvertTo-Json -Depth 4 -Compress` over the pwsh-over-SSH transport
// and returns the parsed configuration dict (vendor + product + pod ID
// + services block).
func newConfigShowCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show",
		Short: "Return the full Holodeck appliance configuration dict",
		Long: "show dispatches holodeck.config.show and returns the full\n" +
			"Get-HoloDeckConfig configuration dict (vendor + product +\n" +
			"version + pod ID + services block). Use when the operator\n" +
			"needs the full appliance configuration snapshot; for just\n" +
			"the identifying fields, prefer `meho holodeck about`\n" +
			"(faster, fewer fields).\n\n" +
			"The human render pretty-prints the parsed dict; --json emits\n" +
			"the full OperationResult envelope including the raw config\n" +
			"sub-dict.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck config show --target holorouter-hetzner-dc\n" +
			"  meho holodeck config show --target holorouter-hetzner-dc --json | jq .result.config",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runConfigShow(cmd, targetName, jsonOut, backplaneOverride)
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

func runConfigShow(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.config.show", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.config.show", r, jsonOut, printConfigShow)
}

func printConfigShow(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.config.show — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	flat, err := decodeFlatResult(r.Result)
	if err != nil || flat == nil {
		fallbackResultRender(w, r)
		return
	}
	if errStr, ok := flat["error"].(string); ok && errStr != "" {
		fmt.Fprintf(w, "  error: %s\n", errStr)
		return
	}
	cfg, ok := flat["config"].(map[string]any)
	if !ok || cfg == nil {
		fallbackResultRender(w, r)
		return
	}
	// Pretty-print the parsed config. The dict shape varies by
	// appliance release (Get-HoloDeckConfig is undocumented stable);
	// json.MarshalIndent surfaces every field without forcing a per-
	// version field map here.
	rawCfg, _ := json.MarshalIndent(cfg, "", "  ")
	for _, line := range splitLines(string(rawCfg)) {
		fmt.Fprintln(w, "  "+line)
	}
}
