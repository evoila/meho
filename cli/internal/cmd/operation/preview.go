// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// PreviewResult mirrors the backend preview envelope — the
// `dict[str, Any]` returned by POST /api/v1/operations/preview
// (:func:`preview_operation`). Hand-written for the same reason as
// CallResult / ResultQueryResult: the FastAPI route types its return as
// `dict[str, Any]`, so the oapi-codegen generator emits
// `*map[string]interface{}` with no typed model worth using.
//
// On `status="ok"` the envelope carries the literal would-be request
// (`method` / `resolved_path` / `query` / `redacted_body`) plus the
// `preview_hash` (#3197) — the stable binding a caller presents on the
// subsequent governed `call_operation` of a `destructive`-tier op. On
// `status="error"` / `status="unavailable"` the `error` string and the
// `extras.error_code` describe why no preview (and no hash) was produced.
// `query` / `redacted_body` / `extras` stay `json.RawMessage` so the
// renderer pretty-prints them without imposing a per-vendor shape.
type PreviewResult struct {
	Status       string          `json:"status"`
	OpID         string          `json:"op_id"`
	ConnectorID  string          `json:"connector_id"`
	SourceKind   string          `json:"source_kind"`
	Method       string          `json:"method"`
	ResolvedPath string          `json:"resolved_path"`
	Query        json.RawMessage `json:"query,omitempty"`
	RedactedBody json.RawMessage `json:"redacted_body,omitempty"`
	PreviewHash  string          `json:"preview_hash"`
	Error        *string         `json:"error"`
	Extras       json.RawMessage `json:"extras,omitempty"`
}

// newPreviewCmd returns the `meho operation preview` command — the
// read-only diagnosis sibling of `meho operation call`, and the CLI
// twin of the MCP `preview_operation` tool. It is the operator's only
// way to obtain the `preview_hash` the destructive tier requires
// (#3197): a `safety_level='destructive'` op refuses to dispatch
// without a `--preview-hash` from a prior preview of the identical
// (connector_id, op_id, target, params).
//
// CLI shape:
//
//	meho operation preview <connector_id> <op_id> \
//	  [--target <name>]                        # target name (required for ops that read a target)
//	  [--params '<json>' | @<file>]            # operation params (object)
//	  [--json]                                 # machine-readable output
//	  [--backplane <url>]                      # override the backplane URL
//
// Exit codes mirror `meho operation call`'s gate-failed semantic — a
// preview resolves the SAME op + target + params but never sends the
// request, so operator-input faults ride the envelope, not transport:
//   - 0   preview resolved (status == "ok") — the `preview_hash` is printed
//   - 1   preview reported status == "error" or status == "unavailable"
//     (unknown op, invalid params, or a typed/composite op with no
//     literal HTTP request to preview)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. unknown / missing status)
func newPreviewCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "preview <connector_id> <op_id>",
		Short: "Resolve an op to its would-be request + preview_hash, without sending",
		Long: "preview invokes POST /api/v1/operations/preview — the read-only " +
			"diagnosis sibling of `meho operation call` and the CLI twin of the " +
			"MCP `preview_operation` tool. It resolves the SAME op + target + " +
			"params a real call would, then returns the literal would-be HTTP " +
			"request (`method` / `resolved_path` / `query` / redacted `body`) " +
			"and a `preview_hash` INSTEAD of dispatching. Nothing is sent and " +
			"no audit row is written.\n\n" +
			"The `preview_hash` is the binding the destructive tier requires " +
			"(#3197): a `safety_level='destructive'` op refuses to dispatch " +
			"unless `meho operation call` carries a `--preview-hash` from a " +
			"prior preview of the IDENTICAL (connector_id, op_id, target, " +
			"params). Run this first, then pass the printed hash to `call`.\n\n" +
			"--target and --params mirror `operation call` exactly. Operator-" +
			"input faults (unknown op, invalid params, unresolvable target) ride " +
			"the envelope as `status=\"error\"` — HTTP is 200. A typed/composite " +
			"op with no single literal HTTP request to preview comes back " +
			"`status=\"unavailable\"` (the destructive-tier composite is the " +
			"exception — it previews a param-bound request tuple).",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPreview(cmd, previewOptions{
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
		"target slug to resolve against (required for ops that read a target)")
	cmd.Flags().StringVar(&paramsFlag, "params", "",
		"operation params as inline JSON or @<file>; omitted means no params")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full preview envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type previewOptions struct {
	ConnectorID       string
	OpID              string
	TargetName        string
	ParamsFlag        string
	JSONOut           bool
	BackplaneOverride string
}

func runPreview(cmd *cobra.Command, opts previewOptions) error {
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
	result, err := postPreview(cmd.Context(), client, opts, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Classify status BEFORE rendering, mirroring `operation call`. The
	// preview envelope's three valid values are "ok" / "error" /
	// "unavailable" (see `_request_preview.py`); anything else is a
	// malformed response — surface as unexpected_response (exit 4)
	// without printing the envelope first, so --json never emits two
	// objects on stdout.
	switch result.Status {
	case "ok", "error", "unavailable":
		// fall through to rendering + exit-code branching below.
	default:
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid preview status %q (expected one of: ok / error / unavailable)",
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
		printPreviewResult(cmd.OutOrStdout(), opts.ConnectorID, opts.OpID, result)
	}
	// "ok" → success (exit 0). "error" / "unavailable" → exit 1 via
	// errOpError so shell pipelines see the gate-failed signal, same as
	// `operation call`.
	if result.Status == "ok" {
		return nil
	}
	return errOpError
}

// postPreview constructs the typed PreviewOperationBody, picks the bare-
// string target shape via FromPreviewOperationBodyTarget0 when --target
// is supplied (mirroring postCall — the CLI never needs the dict-shape
// override), and issues the POST through the generated *WithResponse
// helper. The 401-refresh dance mirrors postCall.
func postPreview(
	ctx context.Context,
	client operationsAPI,
	opts previewOptions,
	params map[string]any,
) (*PreviewResult, error) {
	body := api.PreviewOperationBody{
		ConnectorId: opts.ConnectorID,
		OpId:        opts.OpID,
	}
	if opts.TargetName != "" {
		var target api.PreviewOperationBody_Target
		if err := target.FromPreviewOperationBodyTarget0(opts.TargetName); err != nil {
			return nil, fmt.Errorf("encode target: %w", err)
		}
		body.Target = &target
	}
	if params != nil {
		p := params
		body.Params = &p
	}
	apiParams := &api.PostPreviewApiV1OperationsPreviewPostParams{}
	resp, err := client.PostPreviewApiV1OperationsPreviewPostWithResponse(ctx, apiParams, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.PostPreviewApiV1OperationsPreviewPostWithResponse(ctx, apiParams, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, classifyNon2xx(resp.HTTPResponse, resp.Body)
	}
	var out PreviewResult
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		return nil, fmt.Errorf("decode preview response: %w", err)
	}
	return &out, nil
}

// printPreviewResult renders a PreviewResult as a human-readable header
// line plus the resolved-request projection, then prints the
// `preview_hash` prominently with the ready-to-paste `call` hint — that
// hash is the whole point of the verb (#3197). --json carries the raw
// envelope instead.
func printPreviewResult(w io.Writer, connectorID, opID string, r *PreviewResult) {
	fmt.Fprintf(w, "%s %s — status=%s (source_kind=%s)\n",
		connectorID, opID, r.Status, r.SourceKind)
	if r.Status != "ok" {
		// status == "error" / "unavailable": surface the reason + extras.
		if r.Error != nil && *r.Error != "" {
			fmt.Fprintf(w, "meho: preview %s: %s\n", r.Status, *r.Error)
		}
		if len(r.Extras) > 0 && string(r.Extras) != "null" {
			fmt.Fprintln(w, "extras:")
			printPrettyOrRaw(w, r.Extras)
		}
		return
	}
	fmt.Fprintf(w, "  method:        %s\n", r.Method)
	fmt.Fprintf(w, "  resolved_path: %s\n", r.ResolvedPath)
	if len(r.Query) > 0 && string(r.Query) != "null" {
		fmt.Fprintln(w, "  query:")
		printPrettyOrRaw(w, r.Query)
	}
	if len(r.RedactedBody) > 0 && string(r.RedactedBody) != "null" {
		fmt.Fprintln(w, "  body (redacted):")
		printPrettyOrRaw(w, r.RedactedBody)
	}
	// The hash is the load-bearing output — print it prominently, last,
	// with the exact flag to thread it into a governed call.
	fmt.Fprintf(w, "\npreview_hash: %s\n", r.PreviewHash)
	fmt.Fprintf(w,
		"  → pass to a governed dispatch: meho operation call %s %s --preview-hash %s [--target … --params …]\n",
		connectorID, opID, r.PreviewHash)
}

// printPrettyOrRaw pretty-prints a JSON fragment, falling back to the
// raw bytes when it does not parse. Shared by the query / body / extras
// slots of the preview render.
func printPrettyOrRaw(w io.Writer, raw json.RawMessage) {
	pretty, err := prettyJSON(raw)
	if err == nil {
		fmt.Fprintln(w, pretty)
	} else {
		fmt.Fprintln(w, string(raw))
	}
}
