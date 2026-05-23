// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newConfigCmd returns the `meho pfsense config` parent with one
// sub-verb: `show` (pfsense.config.show).
func newConfigCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "config",
		Short:        "pfSense config sub-verbs (show)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newConfigShowCmd())
	return cmd
}

// newConfigShowCmd returns the `meho pfsense config show` command.
//
// Maps to op_id `pfsense.config.show`. Reads `/cf/conf/config.xml`
// over SSH and returns the raw XML content and its character length.
// For structured gateway data, prefer `meho pfsense network gateway`.
func newConfigShowCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show",
		Short: "Return the full pfSense configuration as XML (/cf/conf/config.xml)",
		Long: "show dispatches pfsense.config.show and returns the raw\n" +
			"config.xml content and its character length. Use when the\n" +
			"operator needs to inspect or export the complete pfSense\n" +
			"configuration. For structured gateway data, prefer:\n" +
			"  meho pfsense network gateway\n\n" +
			"The human render prints the first 40 lines of the XML and\n" +
			"the total length. --json emits the full OperationResult\n" +
			"envelope including the raw config_xml string.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense config show --target pfsense-hetzner-dc\n" +
			"  meho pfsense config show --target pfsense-hetzner-dc --json | jq -r .result.config_xml",
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
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.config.show", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.config.show", r, jsonOut, printConfigShow)
}

func printConfigShow(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.config.show — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	cfg, err := decodeFlatResult(r.Result)
	if err != nil || cfg == nil {
		fallbackResultRender(w, r)
		return
	}
	length := 0
	if l, ok := cfg["length"].(float64); ok {
		length = int(l)
	}
	fmt.Fprintf(w, "  length: %d chars\n", length)

	xmlContent, _ := cfg["config_xml"].(string)
	if xmlContent == "" {
		if errStr, ok := cfg["error"].(string); ok && errStr != "" {
			fmt.Fprintf(w, "  error: %s\n", errStr)
		}
		return
	}
	// Print first 40 lines of XML for the human reader; --json for full content.
	lines := splitLines(xmlContent)
	limit := len(lines)
	if limit > 40 {
		limit = 40
	}
	for _, line := range lines[:limit] {
		fmt.Fprintln(w, "  "+line)
	}
	if len(lines) > 40 {
		fmt.Fprintf(w, "  … (%d more lines — use --json to inspect full XML)\n",
			len(lines)-40)
	}
}

// splitLines splits a string by newlines without the overhead of bufio.
func splitLines(s string) []string {
	if s == "" {
		return nil
	}
	out := make([]string, 0, 32)
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
