// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newK8sCmd returns the `meho holodeck k8s` parent with one sub-verb:
// `exec <kubectl-command>` (holodeck.k8s.exec).
func newK8sCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "k8s",
		Short:        "In-appliance K8s sub-verbs (exec — read-only)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newK8sExecCmd())
	return cmd
}

// newK8sExecCmd returns the `meho holodeck k8s exec <kubectl-cmd>`
// command.
//
// Maps to op_id `holodeck.k8s.exec`. Forwards a **read-only**
// `kubectl` command to the K8s cluster bundled on the HoloRouter
// appliance.
//
// SAFETY-CRITICAL: the CLI passes the operator-supplied command
// argument **verbatim** into `params["command"]` of the typed op.
// The CLI does NOT pre-parse, pre-validate, or sanitise that string
// — the authoritative read-only safelist + shell-metacharacter
// guard live on the backend handler
// (`backend/src/meho_backplane/connectors/holodeck/ops_read.py::parse_kubectl_command`,
// shipped by G3.8-T2 iter-2 #1005). Forwarding the raw string is the
// correct behaviour: duplicating the gate on the client side risks
// drift between CLI and MCP code paths the moment one tightens
// without the other. The backend handler refuses with
// `result_connector_error` if:
//
//   - the verb isn't on the read-only safelist (allowed: get,
//     describe, logs, top, explain, api-resources, api-versions,
//     cluster-info, version);
//   - the command contains POSIX-shell metacharacters
//     (`;` / `&&` / `||` / `|` / `$(...)` / backticks / `>` /
//     `<` / newline / line continuation);
//   - the schema-layer pattern rejects the shape (a guardrail in
//     front of the handler-layer authoritative gate).
//
// Operators see the structured error in the CLI's exit-code-1
// envelope; --json surfaces the full `error` string.
func newK8sExecCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "exec <kubectl-command>",
		Short: "Run a read-only kubectl command on the in-appliance K8s cluster",
		Long: "exec dispatches holodeck.k8s.exec, forwarding the supplied\n" +
			"kubectl command verbatim to the in-appliance K8s cluster on\n" +
			"the HoloRouter appliance via plain SSH (no pwsh indirection).\n\n" +
			"Read-only: only `get`, `describe`, `logs`, `top`, `explain`,\n" +
			"`api-resources`, `api-versions`, `cluster-info`, and `version`\n" +
			"verbs are accepted by the backend. Mutating verbs (create,\n" +
			"apply, delete, edit, replace, patch, scale, rollout, label,\n" +
			"annotate, cp, exec, port-forward, proxy, drain, cordon) and\n" +
			"shell metacharacters (`;` / `&&` / `||` / `|` / `$(...)` /\n" +
			"backticks / `>` / `<` / newline / line continuation) are\n" +
			"refused at the backend with result_connector_error.\n\n" +
			"The CLI does not pre-parse the command — the backend handler\n" +
			"is the authoritative gate. The whole string is passed as\n" +
			"params.command on the wire.\n\n" +
			"The human render surfaces stdout + exit_status; stderr is\n" +
			"shown when non-empty (capped at 4096 chars by the backend).\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Quote the kubectl command so the shell hands it to meho as a\n" +
			"single argv element. Examples below show the recommended\n" +
			"quoting form.\n\n" +
			"Exit codes: 0=ok, 1=error/denied (safety reject or kubectl\n" +
			"failure), 2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho holodeck k8s exec 'kubectl get pods -A' --target holorouter-hetzner-dc\n" +
			"  meho holodeck k8s exec 'kubectl describe node holorouter-node-1' --target holorouter-hetzner-dc\n" +
			"  meho holodeck k8s exec 'kubectl logs -n kube-system <pod>' --target holorouter-hetzner-dc --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runK8sExec(cmd, args[0], targetName, jsonOut, backplaneOverride)
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

func runK8sExec(
	cmd *cobra.Command,
	kubectlCommand, targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	// Pass the operator-supplied command verbatim. The backend handler
	// owns the safety check; the CLI must not pre-parse or pre-validate
	// to avoid drift with the authoritative gate.
	params := map[string]any{"command": kubectlCommand}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "holodeck.k8s.exec", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "holodeck.k8s.exec", r, jsonOut, printK8sExec)
}

func printK8sExec(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s holodeck.k8s.exec — status=%s (%.0fms)\n",
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
	// Safety-reject path: the handler returns status="ok" with an
	// inline error string when the safety check refused the verb.
	// Surface the error string so the operator sees the rejection
	// without needing --json.
	if errStr, ok := flat["error"].(string); ok && errStr != "" {
		fmt.Fprintf(w, "  error: %s\n", errStr)
		return
	}
	exit, _ := flat["exit_status"].(float64)
	fmt.Fprintf(w, "  exit_status: %d\n", int(exit))
	if stdout, ok := flat["stdout"].(string); ok && stdout != "" {
		fmt.Fprintln(w, "  stdout:")
		for _, line := range splitLines(stdout) {
			fmt.Fprintln(w, "    "+line)
		}
	}
	if stderr, ok := flat["stderr"].(string); ok && stderr != "" {
		fmt.Fprintln(w, "  stderr:")
		for _, line := range splitLines(stderr) {
			fmt.Fprintln(w, "    "+line)
		}
	}
}
