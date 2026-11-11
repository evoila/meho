// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package secret

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"

	"github.com/google/uuid"
	"github.com/spf13/cobra"
	"golang.org/x/term"

	"github.com/evoila/meho/cli/internal/api"
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
		approvalID        string
	)
	cmd := &cobra.Command{
		Use:   "read <mount> <path> | read --approval <id>",
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
			"A read that shows a secret value waits for a second person's\n" +
			"approval. Then the command writes no value, prints the request id\n" +
			"on stderr and exits 4. After the approval, run\n" +
			"`meho secret read --approval <id> --field <name>`: it runs the\n" +
			"approved read and writes the value to stdout the same way. Only\n" +
			"the person who sent the request can, once, and only within a short\n" +
			"time after the approval (15 minutes by default). MEHO keeps no\n" +
			"value. Do not run the first command again: that sends a new\n" +
			"request.\n\n" +
			"Exit codes mirror meho operation call: 0=ok, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected or still waiting,\n" +
			"5=not the person who sent the request.",
		Example: "  meho secret read --target vault-lab secret app/db --field password | sshpass -f /dev/stdin ssh root@host\n" +
			"  PW=$(meho secret read --target vault-lab secret app/db --field password)\n" +
			"  PW=$(meho secret read --approval 3f2b9c1e-0000-4000-8000-000000000000 --field password)",
		Args: func(cmd *cobra.Command, args []string) error {
			if approvalID != "" {
				return cobra.NoArgs(cmd, args)
			}
			return cobra.ExactArgs(2)(cmd, args)
		},
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if approvalID != "" {
				return runReadApproved(cmd, approvalID, field, backplaneOverride)
			}
			return runRead(cmd, targetName, args[0], args[1], field, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"Vault target slug to dispatch against (resolved server-side)")
	cmd.Flags().StringVar(&field, "field", "",
		"key within the secret's data map whose raw value is written to stdout (required)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	cmd.Flags().StringVar(&approvalID, "approval", "",
		"id of an approved read request: run it once and write its --field value instead of sending a new read")
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

	// mount + path, plus reveal_secret: this verb's whole contract is to
	// pipe the raw credential value onward, so it opts out of the
	// dispatcher's default credential_read response scrub (#2467). Without
	// it the backplane returns a key-name-scrubbed envelope and --field
	// would extract the redaction sentinel instead of the secret. The
	// dispatcher strips reveal_secret before validating against the op
	// schema and stamps the reveal on the audit row (the field name is
	// still extracted client-side, so it never crosses the wire).
	params := map[string]any{"mount": mount, "path": path, "reveal_secret": true}
	r, err := vaultConn.Call(cmd.Context(), backplaneURL, opKVRead, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, false)
	}

	// A non-ok dispatch (error / denied / awaiting_approval / invalid
	// status) never yields a value. Route it through the shared render so
	// the diagnostic lands on stderr and the exit code matches the rest of
	// the CLI — but pin the output writer to stderr so no envelope text
	// leaks onto the value channel.
	return writeFieldValue(cmd, r, field)
}

// writeFieldValue writes the --field value of an ok read result to
// stdout, or routes a non-ok result to stderr. It is the one place both
// a direct read and the read of an approved request write the value.
func writeFieldValue(cmd *cobra.Command, r *dispatch.CallResult, field string) error {
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
		// vault.kv.read with reveal_secret waits for a second person's
		// approval, so a read lands here until someone approves it. Parked
		// = no value, exit non-zero. After the approval, --approval runs the
		// read once for the requester.
		fmt.Fprintf(cmd.ErrOrStderr(),
			"meho: read parked at status=%s; no value written (%s)\n",
			r.Status, dispatch.ParkedHint)
		if id := dispatch.ParkedApprovalID(r.Extras); id != "" {
			fmt.Fprintf(cmd.ErrOrStderr(),
				"meho: after the approval, get the value with: "+
					"meho secret read --approval %s --field <name>\n", id)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("read dispatch parked awaiting approval; no value available"), false)
	}
	vaultConn.PrintGeneric(cmd.ErrOrStderr(), opKVRead, r)
	// error / denied / rate_limited / not_found → exit 1 via the shared
	// sentinel; any other status is one this CLI does not know (likely a
	// newer backplane) → exit 4.
	switch r.Status {
	case "error", "denied", dispatch.StatusRateLimited, dispatch.StatusNotFound:
		return dispatch.ErrOpError
	default:
		return output.RenderError(cmd.ErrOrStderr(), dispatch.UnknownStatusError(r.Status), false)
	}
}

// runReadApproved writes the --field value of an approved read request.
// It calls POST /api/v1/approvals/{id}/reveal: the backplane runs the
// approved read once, for the person who sent the request only, and
// within a short time after the approval. MEHO keeps no value, so the
// command works once per approval. The pipe-only guard and the
// stdout/stderr split are the same as runRead.
func runReadApproved(cmd *cobra.Command, idArg, field, backplaneOverride string) error {
	if stdoutIsTTY() {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(
			"refusing to write a raw secret to a terminal; `meho secret read` is "+
				"pipe-only — redirect or pipe stdout (e.g. `$(meho secret read …)`)",
		), false)
	}
	requestID, err := uuid.Parse(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--approval %q is not a request id: %v", idArg, err)), false)
	}
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), false)
	}
	client, err := api.NewAuthedClient(cmd.Context(), backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, false)
	}
	view, err := fetchApprovedReveal(cmd.Context(), client, requestID)
	if err != nil {
		var apiErr *dispatch.APIResponseError
		if errors.As(err, &apiErr) {
			switch apiErr.StatusCode {
			case http.StatusForbidden:
				return output.RenderError(cmd.ErrOrStderr(), output.InsufficientRole(
					"not_request_owner: only the person who sent this request can read its value"), false)
			case http.StatusConflict:
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(
					"no value: "+revealRefusal(apiErr.Body)), false)
			}
		}
		return renderRequestError(cmd, backplaneURL, err, false)
	}
	r, serr := approvedReadResult(view)
	if serr != nil {
		return output.RenderError(cmd.ErrOrStderr(), serr, false)
	}
	return writeFieldValue(cmd, r, field)
}

// statusTokenExpired is HTTP 401: the stored token has expired, so the
// call is retried once after a refresh.
const statusTokenExpired = 401

// fetchApprovedReveal calls POST /api/v1/approvals/{id}/reveal with one
// retry after a token refresh. A non-2xx answer comes back as a
// structured error, so the caller never mistakes it for a result.
func fetchApprovedReveal(
	ctx context.Context, client *api.AuthedClient, requestID uuid.UUID,
) (*api.ApprovalRevealResult, error) {
	resp, err := client.RevealApprovedRequestApiV1ApprovalsRequestIdRevealPostWithResponse(ctx, requestID, nil)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == statusTokenExpired {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.RevealApprovedRequestApiV1ApprovalsRequestIdRevealPostWithResponse(ctx, requestID, nil)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 || resp.JSON200 == nil {
		return nil, &dispatch.APIResponseError{StatusCode: resp.StatusCode(), Body: string(resp.Body)}
	}
	return resp.JSON200, nil
}

// revealRefusal is the plain reason of a refused fetch: the "detail" of
// the 409 answer ("reveal_<code>: <message>"), or the raw body.
func revealRefusal(body string) string {
	var parsed struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal([]byte(body), &parsed); err == nil && parsed.Detail != "" {
		return parsed.Detail
	}
	return body
}

// approvedReadResult turns the answer of the reveal route into the read
// result. It fails when the request is not a vault.kv.read.
func approvedReadResult(v *api.ApprovalRevealResult) (*dispatch.CallResult, *output.StructuredError) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, output.Unexpected(fmt.Sprintf("encode reveal result: %v", err))
	}
	var r dispatch.CallResult
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, output.Unexpected(fmt.Sprintf("decode reveal result: %v", err))
	}
	if r.OpID != opKVRead {
		return nil, output.Unexpected(fmt.Sprintf(
			"request %s is not a secret read (op_id=%q)", v.ApprovalRequestId, r.OpID))
	}
	return &r, nil
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
