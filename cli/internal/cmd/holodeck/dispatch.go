// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

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
// Same shape as cmd/pfsense/CallResult; duplicated here because
// cmd/holodeck can't import cmd/pfsense without an import cycle
// (cmd/root.go grafts both onto the tree).
type CallResult struct {
	Status     string          `json:"status"`
	OpID       string          `json:"op_id"`
	Result     json.RawMessage `json:"result"`
	Error      *string         `json:"error"`
	Extras     json.RawMessage `json:"extras,omitempty"`
	DurationMs float64         `json:"duration_ms"`
}

// callRequestBody mirrors the backend CallOperationBody Pydantic
// model. Target uses a map[string]any so the empty case serialises
// as `null` rather than an empty struct.
type callRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// errOpError is the sentinel returned when the dispatcher reported a
// structured-failure result (status == "error" or status == "denied").
var errOpError = errors.New("operation status not ok")

// dispatchOp POSTs an OperationCall to the backplane and returns the
// decoded CallResult. The pre-baked connector_id ("holodeck-ssh-9.0")
// is baked in; callers pass op_id, an optional target slug (empty
// string → no target field on the wire), and an optional params map.
//
// Centralised here so every verb in the package shares one
// dispatcher implementation. A grep for `dispatchOp` finds every
// alias-verb dispatch in the holodeck package.
//
// NOTE: holodeck.k8s.exec passes the operator's `kubectl` command
// verbatim in `params["command"]`. The CLI does not pre-parse or
// pre-validate that string — the read-only safelist + shell-
// metacharacter guard live on the backend handler. Any operator
// invocation containing `;` / `&&` / `|` / `$(...)` / backticks /
// `>` / `<` / newline is refused with `result_connector_error` and
// surfaces as a non-ok status on the CLI side.
func dispatchOp(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, error) {
	body := callRequestBody{
		ConnectorID: ConnectorID,
		OpID:        opID,
		Target:      nil,
	}
	if targetSlug != "" {
		body.Target = map[string]any{"name": targetSlug}
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

// renderCallResult handles the unified post-dispatch path every verb
// uses: validate status enum, render the envelope (JSON or human),
// then translate "error" / "denied" into the errOpError sentinel so
// main propagates the right non-zero exit.
func renderCallResult(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	switch r.Status {
	case "ok", "error", "denied":
		// fall through.
	default:
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid OperationResult.status %q (expected one of: ok / error / denied)",
				r.Status,
			)),
			jsonOut,
		)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), r); err != nil {
			return err
		}
	} else if prettyPrinter != nil {
		prettyPrinter(cmd.OutOrStdout(), r)
	} else {
		printGenericResult(cmd.OutOrStdout(), opID, r)
	}
	if r.Status == "ok" {
		return nil
	}
	return errOpError
}

// printGenericResult renders a CallResult in a generic envelope shape.
func printGenericResult(w io.Writer, opID string, r *CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
	if r.Status == "ok" {
		if len(r.Result) > 0 && string(r.Result) != "null" {
			pretty, err := prettyJSON(r.Result)
			if err == nil {
				fmt.Fprintln(w, pretty)
				return
			}
			fmt.Fprintln(w, string(r.Result))
		}
		return
	}
	printErrorTrailer(w, r)
}

// printErrorTrailer surfaces the dispatcher error / extras envelope.
func printErrorTrailer(w io.Writer, r *CallResult) {
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

// decodeRowsResult decodes the canonical `{"rows": [...], "total": N}`
// envelope every set-shaped Holodeck read op returns
// (holodeck.pod.list, holodeck.service.list).
func decodeRowsResult(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var envelope struct {
		Rows []map[string]any `json:"rows"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, fmt.Errorf("decode rows: %w", err)
	}
	return envelope.Rows, nil
}

// decodeFlatResult decodes a flat-dict result (holodeck.about,
// holodeck.config.show, holodeck.pod.info, holodeck.networking.show,
// holodeck.logs.tail, holodeck.k8s.exec).
func decodeFlatResult(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("decode flat: %w", err)
	}
	return m, nil
}
