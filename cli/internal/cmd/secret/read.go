// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package secret

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// opKVRead is the canonical op_id `meho secret read` dispatches. It is
// the same audited KV-v2 read op `meho vault kv read` uses (registered
// by G3.3-T1 #545); `read` reuses it verbatim so the credential access
// lands on the standard /api/v1/operations/call audit row rather than a
// local, un-audited Vault read (#2146).
const opKVRead = "vault.kv.read"

// vaultConnectorID is the connector_id the KV-v2 read op is registered
// against. `secret read` dispatches this op against `vault-1.x`, NOT the
// package's own `secret-broker-1.x` (which `secret move` uses) — the read
// is a thin client-side wrapper over the existing Vault read, deliberately
// with no new broker op (see #2146 "Out of scope"). A package-local
// dispatch.New is bound here because the cmd/* packages can't import one
// another (import cycle), so the vault package's `conn` isn't reachable.
const vaultConnectorID = "vault-1.x"

// vaultConn binds the KV read op's connector_id to the shared dispatch
// core, separate from this package's secret-broker `conn`.
var vaultConn = dispatch.New(vaultConnectorID)

// stdoutIsTTY is the injectable seam test code uses to fake the presence
// (or absence) of a real terminal on stdout. The default implementation
// interrogates os.Stdout's file descriptor via golang.org/x/term, the
// canonical Go answer for "is this an interactive shell?"
// (devops_best_practices.md §CLI conventions: raw machine-readable output
// only when piped, never onto a live terminal). It mirrors the stdinIsTTY
// seam in cli/internal/cmd/runbook/runbook.go, but guards stdout because
// this verb's contract is "pipe-only": a raw secret must never hit a
// terminal.
var stdoutIsTTY = defaultStdoutIsTTY

// defaultStdoutIsTTY returns true when stdout is a real terminal. The
// indirection through a named function (not a literal in the stdoutIsTTY
// var initializer) lets tests reset the var to production behaviour after
// overriding it.
func defaultStdoutIsTTY() bool {
	return term.IsTerminal(int(os.Stdout.Fd()))
}

// newReadCmd returns the `meho secret read` command: the pipe-only
// emergency credential path. It dispatches the audited vault.kv.read op,
// extracts a single field client-side, and writes ONLY that raw value to
// stdout — no key name, no envelope, no quoting, no trailing newline — so
// `$(meho secret read …)` / a piped sshpass-class consumer gets exactly
// the credential bytes and nothing else.
func newReadCmd() *cobra.Command {
	var (
		targetName        string
		field             string
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "read <mount> <path>",
		Short: "Pipe a single raw secret field to stdout (pipe-only, audited)",
		Long: "read dispatches the audited op_id=\"vault.kv.read\" against\n" +
			"connector_id=\"vault-1.x\" (the same op `meho vault kv read` uses),\n" +
			"extracts the --field value client-side, and writes ONLY that raw\n" +
			"value to stdout — no key name, no envelope, no quoting, no trailing\n" +
			"newline. It exists for the emergency credential path: a value fed\n" +
			"straight into `$(…)` or a pipe (e.g. an sshpass-class consumer)\n" +
			"without the jq-against-the-envelope dance that, under incident\n" +
			"pressure, can print the whole secret onto the terminal.\n\n" +
			"Pipe-only: if stdout is a real terminal the command refuses,\n" +
			"prints the refusal to stderr, and emits NO value — a fat-fingered\n" +
			"invocation yields a refusal, not a password. Redirect or pipe\n" +
			"stdout to use it.\n\n" +
			"All diagnostics and errors go to stderr, so a piped consumer never\n" +
			"consumes an error string as a secret; on any failure stdout stays\n" +
			"empty. The read is audited server-side by vault.kv.read's standard\n" +
			"/api/v1/operations/call audit row (mount + path); the --field name\n" +
			"is NOT recorded (extraction is client-side).\n\n" +
			"Exit codes mirror meho operation call: 0=ok, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho secret read --target rdc-vault secret app/db --field password | sshpass -f /dev/stdin ssh root@host\n" +
			"  PW=$(meho secret read --target rdc-vault secret app/db --field password)",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRead(cmd, targetName, args[0], args[1], field, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"Vault target slug to dispatch against (resolved server-side)")
	cmd.Flags().StringVar(&field, "field", "",
		"key within the secret's data map whose raw value is written to stdout (required)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("field"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// runRead resolves the backplane, refuses on a TTY, dispatches the
// audited vault.kv.read op, and writes the extracted raw field value to
// stdout. Every error path writes to stderr only and leaves stdout empty.
//
// The --json envelope path secret move offers is deliberately absent: this
// verb's whole contract is a bare value on stdout. An operator who wants
// the envelope uses `meho vault kv read --json`.
func runRead(
	cmd *cobra.Command,
	targetName, mount, path, field, backplaneOverride string,
) error {
	// Guardrail 1 — pipe-only. Refuse before dispatching so a
	// fat-fingered interactive invocation never even fetches the
	// credential, let alone prints it. The refusal is a plain stderr line
	// (never --json — this verb has no --json surface) and exits non-zero
	// with nothing on stdout.
	if stdoutIsTTY() {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(
			"refusing to write a raw secret to a terminal; `meho secret read` is "+
				"pipe-only — redirect or pipe stdout (e.g. `$(meho secret read …)`)",
		), false)
	}

	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), false)
	}

	// Same params `meho vault kv read` sends: mount + path. The field is
	// extracted client-side, so it never crosses the wire and is not part
	// of the audit row.
	params := map[string]any{"mount": mount, "path": path}
	r, err := vaultConn.Call(cmd.Context(), backplaneURL, opKVRead, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, false)
	}

	// A non-ok dispatch (error / denied / awaiting_approval / invalid
	// status) never yields a value. Route it through the shared render so
	// the diagnostic lands on stderr and the exit code matches the rest of
	// the CLI — but pin the output writer to stderr so no envelope text
	// leaks onto the value channel.
	if r.Status != "ok" {
		return renderNonOKToStderr(cmd, r)
	}

	value, err := extractField(r.Result, field)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), false)
	}

	// The one and only thing this verb ever writes to stdout: the raw
	// value bytes, nothing added. No key, no envelope, no quoting, no
	// trailing newline — a piped consumer gets exactly the credential.
	fmt.Fprint(cmd.OutOrStdout(), value)
	return nil
}

// renderNonOKToStderr surfaces a non-ok dispatch result on stderr and
// returns the matching exit code, without ever touching stdout. It reuses
// the shared PrintGeneric envelope renderer (which already knows the
// awaiting_approval + error/denied shapes) but pins its writer to stderr,
// then maps the status to an exit code the same way dispatch.Render does.
func renderNonOKToStderr(cmd *cobra.Command, r *dispatch.CallResult) error {
	if r.Status == dispatch.StatusAwaitingApproval {
		// vault.kv.read is not approval-gated, so this is not expected in
		// practice; handle it defensively rather than mis-classifying it
		// as an invalid status. Parked = no value, exit non-zero (unlike
		// secret move's exit-0 park, there is nothing to hand back here).
		fmt.Fprintf(cmd.ErrOrStderr(),
			"meho: read parked at status=%s; no value written (%s)\n",
			r.Status, dispatch.ParkedHint)
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("read dispatch parked awaiting approval; no value available"), false)
	}
	vaultConn.PrintGeneric(cmd.ErrOrStderr(), opKVRead, r)
	// error / denied → exit 1 via the shared sentinel; any other status is
	// a backend contract violation → exit 4.
	switch r.Status {
	case "error", "denied":
		return dispatch.ErrOpError
	default:
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf(
			"backplane returned invalid OperationResult.status %q", r.Status)), false)
	}
}

// extractField decodes the vault.kv.read result envelope ({data: {...},
// version: N}) and returns the raw string value at data[field]. It fails
// closed: a missing field, a null value, or a non-string/non-scalar value
// is an error (written to stderr by the caller), never a partial or coerced
// value on stdout.
func extractField(raw json.RawMessage, field string) (string, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return "", fmt.Errorf("read returned an empty result envelope; no %q field to extract", field)
	}
	var env struct {
		Data map[string]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return "", fmt.Errorf("decode read result envelope: %w", err)
	}
	if env.Data == nil {
		return "", fmt.Errorf("read result has no data map; cannot extract field %q", field)
	}
	rawVal, ok := env.Data[field]
	if !ok {
		return "", fmt.Errorf("field %q not present in the secret at this path", field)
	}
	return scalarToString(rawVal, field)
}

// scalarToString renders a single JSON scalar (string, number, or bool)
// as its raw string form for stdout. A string yields its unquoted bytes; a
// number or bool yields its literal JSON text (which is already the raw
// form). A null, object, or array is rejected — those are not a single
// credential value and must not be silently stringified onto the value
// channel.
func scalarToString(raw json.RawMessage, field string) (string, error) {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return "", fmt.Errorf("decode field %q: %w", field, err)
	}
	switch tv := v.(type) {
	case string:
		return tv, nil
	case bool:
		if tv {
			return "true", nil
		}
		return "false", nil
	case float64:
		// json.Number-free path: re-render the original raw bytes rather
		// than the float64 round-trip, so integers and large / precise
		// numbers keep their exact source text (e.g. "12345678901234567").
		return string(raw), nil
	case nil:
		return "", fmt.Errorf("field %q is null; no value to write", field)
	default:
		return "", fmt.Errorf("field %q is not a scalar value (got a JSON object/array); "+
			"secret read writes single credential fields only", field)
	}
}
