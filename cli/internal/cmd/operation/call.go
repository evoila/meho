// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
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
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes (mirrors `meho retrieval eval`'s gate-failed semantic
// for the structured-failure case — dispatcher errors come back in
// the `error` / `extras` envelope, not as transport failures):
//   - 0   operation invoked + status == "ok"
//   - 1   operation invoked but status == "error" or status == "denied"
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. unknown / missing status)
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
	result, err := postCall(cmd.Context(), client, opts, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Classify status BEFORE rendering. The backend
	// `Connector.execute` contract (see
	// `backend/src/meho_backplane/connectors/schemas.py`) defines three
	// valid values: "ok" / "error" / "denied". Anything else is a
	// malformed response — surface as unexpected_response (exit 4)
	// without printing the result envelope first. Pre-fix-3 we
	// rendered then classified, which (a) misled the operator into
	// thinking they had a real result before the trailing error fired
	// and (b) produced two JSON objects on stdout in --json mode,
	// breaking pipe-into-jq usage.
	switch result.Status {
	case "ok", "error", "denied":
		// fall through to rendering + exit-code branching below.
	default:
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid OperationResult.status %q (expected one of: ok / error / denied)",
				result.Status,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), result); err != nil {
			return err
		}
	} else {
		printCallResult(cmd.OutOrStdout(), opts.ConnectorID, opts.OpID, result)
	}
	// "ok" → success (exit 0). "error" / "denied" → exit 1 via
	// errOpError so shell pipelines see the gate-failed signal.
	if result.Status == "ok" {
		return nil
	}
	return errOpError
}

// errOpError is the sentinel returned when the dispatcher reported
// a structured-failure result (status == "error" or status ==
// "denied"). main translates non-nil RunE errors into a non-zero
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
	body := api.CallOperationBody{
		ConnectorId: opts.ConnectorID,
		OpId:        opts.OpID,
	}
	if opts.TargetName != "" {
		var target api.CallOperationBody_Target
		if err := target.FromCallOperationBodyTarget0(opts.TargetName); err != nil {
			return nil, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &target
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	apiParams := &api.PostCallApiV1OperationsCallPostParams{}
	resp, err := client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.PostCallApiV1OperationsCallPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out CallResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
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
