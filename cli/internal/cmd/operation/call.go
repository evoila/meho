// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// CallResult mirrors the backend OperationResult Pydantic model.
// Result is left as json.RawMessage because the backend types it as
// a oneOf(dict, list) union — pretty-printing the raw bytes is the
// cleanest renderer until callers grow a need for per-shape
// specialisation. Same approach `retrieval.EvalResult` takes for
// `Thresholds`.
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// callRequestBody mirrors the backend CallOperationBody Pydantic
// model. Target uses a map[string]any so the empty case (typed
// handlers that don't need a target) can serialise as `null` rather
// than an empty struct; the route layer's resolver short-circuits
// on the missing-name case (raising 400) for the few ops that do
// need a target.
type callRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// newCallCmd returns the `meho operation call` command.
//
// CLI shape:
//
//	meho operation call <connector_id> <op_id> \
//	  --target <slug>                          # target name (required)
//	  [--params '<json>' | @<file>]            # operation params (object)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes (mirrors `meho retrieval eval`'s gate-failed semantic
// for the status != "ok" case — dispatcher errors come back in the
// `error` / `extras` envelope, not as transport failures):
//   - 0   operation invoked + status == "ok"
//   - 1   operation invoked but status == "error"
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape
func newCallCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
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
			"\"ok\" on success or \"error\" on a connector-side failure; the " +
			"HTTP status is 200 in both cases, so dispatcher errors don't " +
			"masquerade as transport errors.\n\n" +
			"--params accepts inline JSON (`--params '{\"path\":\"secret/x\"}'`) " +
			"or a file reference (`--params @./params.json`). The empty case " +
			"(`--params` omitted) sends no params key on the wire — typed " +
			"handlers that don't read params see an empty mapping at the " +
			"validation layer.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCall(cmd, callOptions{
				ConnectorID:       args[0],
				OpID:              args[1],
				TargetName:        targetName,
				ParamsFlag:        paramsFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required for ops that read a target)")
	cmd.Flags().StringVar(&paramsFlag, "params", "",
		"operation params as inline JSON or @<file>; omitted means no params")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type callOptions struct {
	ConnectorID       string
	OpID              string
	TargetName        string
	ParamsFlag        string
	JSONOut           bool
	BackplaneOverride string
}

func runCall(cmd *cobra.Command, opts callOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	params, err := loadParamsFlag(opts.ParamsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	result, err := postCall(cmd.Context(), backplaneURL, opts, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), result); err != nil {
			return err
		}
	} else {
		printCallResult(cmd.OutOrStdout(), opts.ConnectorID, opts.OpID, result)
	}
	// Exit non-zero on dispatcher-side error so shell pipelines see
	// the gate-failed signal. Same shape as retrieval/eval.go's
	// errEvalGate sentinel.
	if result.Status != "ok" {
		return errOpError
	}
	return nil
}

// errOpError is the sentinel returned when the dispatcher reported
// a structured-error result (status != "ok"). main translates non-
// nil RunE errors into a non-zero exit; cobra's SilenceErrors is
// true on the command so the error string isn't double-printed.
// Same shape as retrieval/eval.go's errEvalGate.
var errOpError = errors.New("operation status != ok")

func postCall(
	ctx context.Context,
	backplaneURL string,
	opts callOptions,
	params map[string]any,
) (*CallResult, error) {
	body := callRequestBody{
		ConnectorID: opts.ConnectorID,
		OpID:        opts.OpID,
		Target:      nil,
	}
	if opts.TargetName != "" {
		body.Target = map[string]any{"name": opts.TargetName}
	}
	if params != nil {
		body.Params = params
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal call request: %w", err)
	}
	respBody, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/operations/call", raw)
	if err != nil {
		return nil, err
	}
	var out CallResult
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, fmt.Errorf("decode call response: %w", err)
	}
	return &out, nil
}

func printCallResult(w io.Writer, connectorID, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", connectorID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := prettyJSON(r.Result)
			if err == nil {
				fmt.Fprintln(w, pretty)
				return
			}
			// Fallback: raw bytes when pretty-printing failed.
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	// status == "error" (or any non-ok dispatcher envelope).
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := prettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}

func prettyJSON(raw json.RawMessage) (string, error) {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return "", err
	}
	out, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return "", err
	}
	return string(out), nil
}
