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

// newPodCmd returns the `meho holodeck pod` parent with two sub-verbs:
// `list` (holodeck.pod.list) and `info <pod-id>` (holodeck.pod.info).
func newPodCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "pod",
		Short:        "Holodeck nested-pod sub-verbs (list, info)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newPodListCmd())
	cmd.AddCommand(newPodInfoCmd())
	return cmd
}

// newPodListCmd returns the `meho holodeck pod list` command.
//
// Maps to op_id `holodeck.pod.list`. Runs `Get-HoloDeckPod |
// ConvertTo-Json -Depth 4` over the pwsh-over-SSH transport and
// returns a JSONFlux-shaped `{rows, total}` envelope. The JSONFlux
// handle itself is the reducer's responsibility; the CLI surfaces
// rows inline today.
//
// This is the 1:1 replacement for the consumer's
// `./scripts/holodeck.sh --target holorouter 'pwsh -c
// "Get-HoloDeckPod | Format-Table"'` invocation.
func newPodListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List active Holodeck nested pods (Get-HoloDeckPod)",
		Long: "list dispatches holodeck.pod.list and renders one row per\n" +
			"active nested pod (pod ID / name / state / primary network /\n" +
			"VM count). Returns the JSONFlux-shaped `{rows, total}`\n" +
			"envelope; future JSONFlux reducer will spill large pod lists\n" +
			"to the HandleStore via the standard result_describe /\n" +
			"result_query flow.\n\n" +
			"The human render caps at 20 rows; --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck pod list --target holorouter-hetzner-dc\n" +
			"  meho holodeck pod list --target holorouter-hetzner-dc --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runPodList(cmd, targetName, jsonOut, backplaneOverride)
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

func runPodList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.pod.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.pod.list", r, jsonOut, printPodList)
}

func printPodList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.pod.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	limit := len(rows)
	truncated := false
	if limit > 20 {
		limit = 20
		truncated = true
	}
	fmt.Fprintf(w, "  %-20s %-20s %-10s %s\n", "POD-ID", "NAME", "STATE", "NETWORK")
	for _, row := range rows[:limit] {
		podID := stringField(row, "Id")
		if podID == "" {
			podID = stringField(row, "PodId")
		}
		name := stringField(row, "Name")
		state := stringField(row, "State")
		network := truncate(stringField(row, "Network"), 30)
		fmt.Fprintf(w, "  %-20s %-20s %-10s %s\n",
			truncate(podID, 20), truncate(name, 20), state, network)
	}
	if truncated {
		fmt.Fprintf(w, "  … (%d more rows — use --json to inspect all)\n", len(rows)-20)
	}
	fmt.Fprintf(w, "  (%d pods total)\n", len(rows))
}

// newPodInfoCmd returns the `meho holodeck pod info <pod-id>` command.
//
// Maps to op_id `holodeck.pod.info`. Runs `Get-HoloDeckPod -Id '<id>'
// | ConvertTo-Json -Depth 4` over the pwsh-over-SSH transport and
// returns the single-pod detail dict (state, networking, VMs).
//
// pod-id is a positional argument so the command line reads
// `meho holodeck pod info HoloPod-001 --target ...` like a normal
// shell verb. The CLI does not interpolate the value into a shell
// pipeline; the backend handler PowerShell-quotes it before
// constructing the cmdlet (`Get-HoloDeckPod -Id '<id>'`).
func newPodInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <pod-id>",
		Short: "Return per-pod detail (state, networking, VMs) for a Holodeck pod",
		Long: "info dispatches holodeck.pod.info and returns the single-pod\n" +
			"detail dict: state, networking (FRR/BGP attachment), and the\n" +
			"embedded VM list with their power state. Use\n" +
			"`meho holodeck pod list` first to enumerate available pod IDs.\n\n" +
			"The human render pretty-prints the parsed dict; --json emits\n" +
			"the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho holodeck pod info HoloPod-001 --target holorouter-hetzner-dc\n" +
			"  meho holodeck pod info HoloPod-001 --target holorouter-hetzner-dc --json | jq .result.pod",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPodInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
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

func runPodInfo(
	cmd *cobra.Command,
	podID, targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"pod_id": podID}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.pod.info", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.pod.info", r, jsonOut, printPodInfo)
}

func printPodInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.pod.info — status=%s (%.0fms)\n",
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
	pod, ok := flat["pod"].(map[string]any)
	if !ok || pod == nil {
		fallbackResultRender(w, r)
		return
	}
	rawPod, _ := json.MarshalIndent(pod, "", "  ")
	for _, line := range splitLines(string(rawPod)) {
		fmt.Fprintln(w, "  "+line)
	}
}
