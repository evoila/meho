// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

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
// Same shape as cmd/vmware/CallResult; duplicated here because
// cmd/bind9 can't import cmd/vmware without an import cycle
// (cmd/root.go grafts both onto the tree).
//
// Result is left as json.RawMessage because the backend types it as
// a oneOf(dict, list) union — pretty-printing the raw bytes is the
// cleanest renderer until a per-shape table specialisation lands.
// Same approach the vmware sibling takes.
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
// as `null` rather than an empty struct; the route layer's resolver
// short-circuits on the missing-name case (raising 400) for ops
// that need a target.
type callRequestBody struct {
	ConnectorID string         `json:"connector_id"`
	OpID        string         `json:"op_id"`
	Target      map[string]any `json:"target"`
	Params      map[string]any `json:"params,omitempty"`
}

// errOpError is the sentinel returned when the dispatcher reported a
// structured-failure result (status == "error" or status ==
// "denied"). main translates non-nil RunE errors into a non-zero
// exit; SilenceErrors=true on each command keeps cobra from double-
// printing the error string. Same shape as the vmware sibling's
// errOpError.
var errOpError = errors.New("operation status not ok")

// dispatchOp POSTs an OperationCall to the backplane and returns the
// decoded CallResult. The pre-baked connector_id ("bind9-ssh-9.x")
// is baked in; callers pass op_id, an optional target slug (empty
// string → no target field on the wire), and an optional params map.
//
// Centralised here so every verb in the package shares one
// dispatcher implementation (over a dozen call sites). Renamed from
// the vmware sibling's postCall to make the call sites self-
// documenting at the grep level: a grep for `dispatchOp` finds every
// alias-verb dispatch in the bind9 package.
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
// main propagates the right non-zero exit. Returns nil on success
// or one of:
//   - errOpError (status == "error" / "denied" → exit 1)
//   - a structured renderer error (unexpected status → exit 4)
//
// Pretty-printing is delegated to a per-verb closure so verb files
// can lay out their domain-specific tables (zone list shows
// name/type/file columns; record get shows the rdata rows; etc.).
// When prettyPrinter is nil, the fallback renders the generic
// envelope shape — handy for verbs whose result envelope is opaque
// to the CLI today (config.apply_* / config.backup write envelopes).
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

// printGenericResult renders a CallResult in the same shape the
// vmware sibling's printGenericResult uses. Used as the fallback
// pretty-printer for verbs that don't define their own table layout
// (config.apply_* writes whose result envelope is opaque to the
// CLI; config.backup whose listing is best read as JSON).
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
// Used by every per-verb pretty-printer's error branch so the
// "status=error" output is consistent across verbs. Same shape as
// the vmware sibling's helper.
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

// prettyJSON pretty-prints a json.RawMessage with 2-space indent.
// Same implementation as the vmware sibling.
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

// decodeRowsResult decodes the canonical `{"rows": [...], "total": N}`
// envelope every set-shaped bind9 read op returns (zone.list,
// zone.read, record.get, config.backup listing). Returns the row list
// or an error when the shape doesn't match — call sites fall back to
// the generic JSON dump on decode failure rather than panicking.
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

// decodeFlatResult decodes a flat-dict result (bind9.about,
// bind9.config.show, bind9.config.reload, bind9.record.add /
// .remove). Returns the decoded map or an error.
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

// stringField pulls a string field from a row entry, returning empty
// string when the field is missing or wrong type. Mirrors the vmware
// sibling's helper.
func stringField(e map[string]any, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// fallbackResultRender dumps the result envelope verbatim when the
// typed per-verb decode fails. Used by every verb's pretty-printer
// so contract drift surfaces with the same affordance.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := prettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}
