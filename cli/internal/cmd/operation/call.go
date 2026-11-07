// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// CallResult mirrors the backend OperationResult Pydantic model.
// Result is left as json.RawMessage because the backend types it as
// a oneOf(dict, list) union — pretty-printing the raw bytes is the
// cleanest renderer until callers grow a need for per-shape
// specialisation. Same approach `retrieval.EvalResult` takes for
// `Thresholds`.
//
// Kept hand-written rather than generator-backed because the FastAPI
// surface types this route's response as `dict[str, Any]` and the
// oapi-codegen generator therefore emits the response as a
// `*map[string]interface{}` (see PostCallApiV1OperationsCallPostResponse
// .JSON200 in client.gen.go) — no typed model worth using. Promoting
// the FastAPI response to a typed model so the generator picks it up
// is a separate backend Task explicitly out of scope for G0.12-T2
// #1260 (Initiative #1118 is consumer-side only).
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Handle     json.RawMessage `json:"handle,omitempty"`
	AuditID    *string         `json:"audit_id"`
	Delivery   *string         `json:"delivery"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// newCallCmd returns the `meho operation call` command.
//
// CLI shape:
//
//	meho operation call <connector_id> <op_id> \
//	  [--target <slug>]                        # target name (required for ops that read a target)
//	  [--params '<json>' | @<file>]            # operation params (object)
//	  [--work-ref gh:<owner>/<repo>#<n>]       # external change-ticket reference
//	  [--preview-hash <hash>]                  # destructive-tier binding from `operation preview` (#3197)
//	  [--async]                                # start in the background, print a run id
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes (mirrors `meho retrieval eval`'s gate-failed semantic
// for the structured-failure case — dispatcher errors come back in
// the `error` / `extras` envelope, not as transport failures):
//   - 0   operation invoked + status == "ok"
//   - 1   operation invoked but status == "error", "denied", "rate_limited" or "not_found"
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. unknown / missing status)
func newCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		workRef           string
		previewHash       string
		jsonOut           bool
		async             bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "call <connector_id> <op_id>",
		Short: "Invoke an operation through the G0.6 dispatcher",
		Long: "call invokes POST /api/v1/operations/call. The dispatcher " +
			"resolves the target via :func:`resolve_target`, validates " +
			"params against the registered endpoint_descriptor schema, " +
			"runs the op, writes an audit row, and returns a structured " +
			"OperationResult envelope. The envelope's `status` field is " +
			"\"ok\" on success or \"error\" / \"denied\" on a dispatcher-" +
			"reported failure (connector raised, schema-validation rejected, " +
			"or policy denied); the HTTP status is 200 in all three cases " +
			"so dispatcher outcomes don't masquerade as transport errors.\n\n" +
			"--target is required for ops that read a target (most vendor " +
			"ops); typed handlers that resolve their own context (e.g. some " +
			"composite handlers) can be invoked without it.\n\n" +
			"--params accepts inline JSON (`--params '{\"path\":\"secret/x\"}'`) " +
			"or a file reference (`--params @./params.json`). The empty case " +
			"(`--params` omitted) sends no params key on the wire — typed " +
			"handlers that don't read params see an empty mapping at the " +
			"validation layer.\n\n" +
			"--preview-hash carries the binding a `safety_level='destructive'` " +
			"op requires (#3197): obtain it from `meho operation preview` of the " +
			"IDENTICAL (connector_id, op_id, target, params), then pass it here. " +
			"The dispatcher fail-closes with status=denied / " +
			"error_code=preview_binding_required when a destructive op is called " +
			"without a matching hash; it is ignored for every non-destructive op.\n\n" +
			"--async starts the operation in the background and returns at once " +
			"with a run id. The command does not wait, so a VPN or proxy limit " +
			"cannot cut the answer. Read the run later, also from another " +
			"terminal, with `meho operation run-status <run id>` (add --wait to " +
			"wait for the end).",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCall(cmd, callOptions{
				ConnectorID:       args[0],
				OpID:              args[1],
				TargetName:        targetName,
				ParamsFlag:        paramsFlag,
				WorkRef:           workRef,
				PreviewHash:       previewHash,
				JSONOut:           jsonOut,
				Async:             async,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required for ops that read a target)")
	cmd.Flags().StringVar(&paramsFlag, "params", "",
		"operation params as inline JSON or @<file>; omitted means no params")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"external change-ticket reference for this dispatch's audit and approval rows (e.g. gh:evoila/meho#13)")
	cmd.Flags().StringVar(&previewHash, "preview-hash", "",
		"preview_hash from a prior `meho operation preview` — required for a destructive-tier op (#3197)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().BoolVar(&async, "async", false,
		"start the operation in the background and return at once with a run id; read it with `meho operation run-status`")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type callOptions struct {
	ConnectorID       string
	OpID              string
	TargetName        string
	ParamsFlag        string
	WorkRef           string
	PreviewHash       string
	JSONOut           bool
	Async             bool
	BackplaneOverride string
}

func runCall(cmd *cobra.Command, opts callOptions) error {
	if opts.WorkRef != "" && !api.ValidWorkRef(opts.WorkRef) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--work-ref must match gh:<owner>/<repo>#<n>"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	params, err := loadParamsFlag(opts.ParamsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result, run, err := sendCall(cmd.Context(), client, opts, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if run != nil {
		return printRunStarted(cmd.OutOrStdout(), run, opts.JSONOut)
	}
	return renderCallOutcome(cmd, opts.ConnectorID, opts.OpID, result, opts.JSONOut, result)
}

// renderCallOutcome prints an OperationResult envelope and returns the
// exit signal. `meho operation call` and `meho operation run-status`
// share it, so a stored result looks and exits like a direct call.
// jsonPayload is what --json prints: the envelope for a call, the whole
// run for run-status.
func renderCallOutcome(
	cmd *cobra.Command,
	connectorID, opID string,
	result *CallResult,
	jsonOut bool,
	jsonPayload any,
) error {
	// awaiting_approval (parked) is a first-class non-error outcome:
	// the dispatcher has durably written an ApprovalRequest row and
	// parked the call (G11.7-T1 #1401). Intercept it ahead of the
	// status switch — mirroring the shared dispatch.Render path the
	// vendor verbs go through — so the generic `operation call` verb
	// renders the parked hint (or the full envelope, incl.
	// extras.approval_request_id, under --json) and exits 0 instead of
	// rejecting it as an invalid status (exit 4).
	if result.Status == dispatch.StatusAwaitingApproval {
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), jsonPayload)
		}
		w := cmd.OutOrStdout()
		fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n",
			connectorID, opID, result.Status, result.DurationMs)
		dispatch.WriteParkedHint(w, result.Extras)
		// A multi-step operation that stops for approval already has
		// its own audit row; show its id like every other status (#4833).
		dispatch.WriteAuditReceipt(w, result.AuditID)
		return nil // parked, not failed — exit 0.
	}
	// Classify status BEFORE rendering. The backend
	// `Connector.execute` contract (see
	// `backend/src/meho_backplane/connectors/schemas.py`) defines
	// "ok" / "error" / "denied" / "rate_limited" / "not_found"
	// (awaiting_approval is handled above).
	// Anything else is a status this CLI does not know — most likely
	// the backplane is newer — so surface it as unexpected_response
	// (exit 4) with a message that names the version gap, without
	// printing the result envelope first. Pre-fix-3 we
	// rendered then classified, which (a) misled the operator into
	// thinking they had a real result before the trailing error fired
	// and (b) produced two JSON objects on stdout in --json mode,
	// breaking pipe-into-jq usage.
	switch result.Status {
	case "ok", "error", "denied", dispatch.StatusRateLimited, dispatch.StatusNotFound:
		// fall through to rendering + exit-code branching below.
	default:
		return output.RenderError(cmd.ErrOrStderr(), dispatch.UnknownStatusError(result.Status), jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), jsonPayload); err != nil {
			return err
		}
	} else {
		PrintCallResult(cmd.OutOrStdout(), connectorID, opID, result)
	}
	// "ok" → success (exit 0). "error" / "denied" / "rate_limited" /
	// "not_found" → exit 1 via errOpError so shell pipelines see the
	// gate-failed signal.
	if result.Status == "ok" {
		return nil
	}
	return errOpError
}

// runAccepted is the HTTP 202 answer of an async call.
type runAccepted struct {
	RunID  string `json:"run_id"`
	Status string `json:"status"`
	Async  bool   `json:"async"`
}

// printRunStarted tells the operator the operation started and how to
// check it. With --json it prints the server's answer as it is.
func printRunStarted(w io.Writer, run *runAccepted, jsonOut bool) error {
	if jsonOut {
		return output.PrintJSON(w, run)
	}
	fmt.Fprintf(w, "Started. Run id: %s. Check it with `meho operation run-status %s`.\n",
		run.RunID, run.RunID)
	return nil
}

// errOpError is the sentinel returned when the dispatcher reported
// a structured-failure result (status == "error", "denied",
// "rate_limited" or "not_found"). main translates non-nil RunE errors into a non-zero
// exit; cobra's SilenceErrors is true on the command so the error
// string isn't double-printed. Same shape as retrieval/eval.go's
// errEvalGate.
var errOpError = errors.New("operation status not ok")

// postCall constructs the typed CallOperationBody, picks the bare-
// string target shape via FromCallOperationBodyTarget0 when --target
// is supplied (the canonical write surface per G0.13-T2 #1132; the
// CLI never needed the dict-shape fqdn override — that's an MCP-
// handler use case), and issues the POST through the generated
// *WithResponse helper. The 401-refresh dance mirrors GetHealth on
// *api.AuthedClient.
func postCall(
	ctx context.Context,
	client operationsAPI,
	opts callOptions,
	params map[string]any,
) (*CallResult, error) {
	result, _, err := sendCall(ctx, client, opts, params)
	return result, err
}

// sendCall is postCall with async support. With opts.Async the server
// answers HTTP 202 and a run id, and sendCall returns the run and a nil
// result. A server that ignores async still answers 200, which is
// returned as a normal result.
func sendCall(
	ctx context.Context,
	client operationsAPI,
	opts callOptions,
	params map[string]any,
) (*CallResult, *runAccepted, error) {
	body := api.CallOperationBody{
		ConnectorId: opts.ConnectorID,
		OpId:        opts.OpID,
	}
	if opts.TargetName != "" {
		var target api.CallOperationBody_Target
		if err := target.FromCallOperationBodyTarget0(opts.TargetName); err != nil {
			return nil, nil, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &target
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		body.WorkRef = &wr
	}
	// Thread the destructive-tier preview binding (#3197). Left nil when
	// --preview-hash is unset so a bare call stays byte-identical to the
	// pre-#3197 wire shape; the field is ignored for non-destructive ops.
	if opts.PreviewHash != "" {
		h := opts.PreviewHash
		body.PreviewHash = &h
	}
	// Left nil without --async so a bare call keeps the old wire shape.
	if opts.Async {
		a := true
		body.Async = &a
	}
	apiParams := &api.PostCallApiV1OperationsCallPostParams{}
	resp, err := client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, nil, err
		}
	}
	if opts.Async && resp.StatusCode() == http.StatusAccepted {
		var run runAccepted
		if err := json.Unmarshal(resp.Body, &run); err != nil {
			return nil, nil, fmt.Errorf("decode async call response: %w", err)
		}
		if run.RunID == "" {
			return nil, nil, errors.New("decode async call response: no run_id")
		}
		return nil, &run, nil
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out CallResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, nil, fmt.Errorf("decode call response: %w", err)
	}
	return &out, nil, nil
}

// ResultHandleID returns the handle_id of a result handle, or "" when there is none.
func ResultHandleID(raw json.RawMessage) string {
	var h struct {
		HandleID string `json:"handle_id"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &h) != nil {
		return ""
	}
	return h.HandleID
}

// PrintCallResult is the human render of an OperationResult envelope.
// `meho approvals result` uses it too, so the stored result of an
// approved run looks the same as a direct call (#5313).
func PrintCallResult(w io.Writer, connectorID, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", connectorID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := prettyJSON(r.Result)
			if err == nil {
				fmt.Fprintln(w, pretty)
			} else {
				// Fallback: raw bytes when pretty-printing failed.
				fmt.Fprintln(w, string(r.Result))
			}
		}
		if len(r.Handle) > 0 && string(r.Handle) != "null" {
			fmt.Fprintln(w, "result handle:")
			pretty, err := prettyJSON(r.Handle)
			if err == nil {
				fmt.Fprintln(w, pretty)
			} else {
				fmt.Fprintln(w, string(r.Handle))
			}
		}
		dispatch.WriteAuditReceipt(w, r.AuditID)
		if r.Delivery != nil {
			fmt.Fprintf(w, "delivery: %s\n", *r.Delivery)
		}
		if r.Delivery != nil && (*r.Delivery == "unavailable" || *r.Delivery == "partial") {
			fmt.Fprintln(w, "delivery incomplete: operation already executed; do not re-invoke it.")
			if *r.Delivery == "partial" {
				if id := ResultHandleID(r.Handle); id != "" {
					fmt.Fprintf(w, "the response was too large to show; read it with `meho operation result-query %s`.\n", id)
				}
			}
			if r.AuditID != nil {
				fmt.Fprintln(w, "inspect the audit receipt or operation status.")
			} else {
				fmt.Fprintln(w, "inspect operation status; no committed audit receipt is available.")
			}
		}
		return
	}
	// status == "error" (or any non-ok dispatcher envelope). The shared
	// trailer prints the connector error, the ids a not_found envelope
	// names, the extras blob and the audit receipt: a failed call has an
	// audit row that records why it failed (#4833).
	dispatch.WriteErrorTrailer(w, &dispatch.CallResult{
		Status: r.Status, OpID: r.OpID, Error: r.Error,
		Extras: r.Extras, AuditID: r.AuditID,
	})
}

// prettyJSON indents the raw JSON as it is. It does not decode it into
// Go values, so large integers keep every digit.
func prettyJSON(raw json.RawMessage) (string, error) {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return "", err
	}
	return out.String(), nil
}
