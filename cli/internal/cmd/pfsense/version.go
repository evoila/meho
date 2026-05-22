// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newVersionCmd returns the `meho pfsense version` command.
//
// Maps to op_id `pfsense.version`. Returns a structured version
// summary (version / build / kernel) without the full FingerprintResult
// envelope that `about` returns.
func newVersionCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "version",
		Short: "Show pfSense version / build / kernel for a target",
		Long: "version dispatches pfsense.version and renders the version /\n" +
			"build / kernel fields. Prefer `about` when the full\n" +
			"FingerprintResult envelope (vendor + product) is needed.\n\n" +
			"Exit codes mirror meho operation call (0=ok, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected).",
		Example: "  meho pfsense version --target pfsense-hetzner-dc\n" +
			"  meho pfsense version --target pfsense-hetzner-dc --json | jq .result.version",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runVersion(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runVersion(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.version", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.version", r, jsonOut, printVersion)
}

func printVersion(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.version — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	ver, err := decodeFlatResult(r.Result)
	if err != nil || ver == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"version", "build", "kernel"} {
		v, ok := ver[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-10s %v\n", key+":", v)
	}
}
