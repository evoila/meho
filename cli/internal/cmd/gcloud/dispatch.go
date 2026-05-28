// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// gcloud was the deviation pre-#1274: it carried its own dispatchOp /
// renderCallResult / printGenericResult trio with no dispatch.go file,
// while every other vendor dir bound to the shared dispatch.Connector
// (with the per-dir doAuthedRequest still injected). G0.12-T16 #1274
// promoted the authed transport into the shared dispatch.Connector and
// folded gcloud onto the same pattern at the same time.
type (
	// CallResult is the decoded OperationResult envelope.
	CallResult = dispatch.CallResult
	// callRequestBody is the on-the-wire OperationCall body (asserted by tests).
	callRequestBody = dispatch.CallRequestBody
)

// errOpError is the structured-failure sentinel (status error/denied).
var errOpError = dispatch.ErrOpError

// conn binds this package's pre-baked connector_id to the shared
// dispatch core. The authed transport (lazy *api.AuthedClient over the
// generated typed surface) lives inside dispatch.Connector after
// G0.12-T16 #1274 promoted the per-vendor doAuthedRequest copies.
var conn = dispatch.New(ConnectorID)

// dispatchOp is a thin wrapper around conn.Call kept so the per-verb
// files (about.go / project.go / services.go / iam.go / compute.go)
// continue calling the unqualified name they were authored against.
func dispatchOp(
	ctx context.Context,
	backplaneURL, opID, targetSlug string,
	params map[string]any,
) (*CallResult, error) {
	return conn.Call(ctx, backplaneURL, opID, targetSlug, params)
}

// renderCallResult is a thin wrapper around conn.Render for the same
// reason as dispatchOp above.
func renderCallResult(
	cmd *cobra.Command,
	opID string,
	r *CallResult,
	jsonOut bool,
	prettyPrinter func(w io.Writer, r *CallResult),
) error {
	return conn.Render(cmd, opID, r, jsonOut, prettyPrinter)
}

// errRowsKeyAbsent is returned by decodeRowsResult when the result
// envelope is a well-formed JSON object that carries no `rows` key at
// all. This is distinct from an empty list (`{"rows": []}`): an absent
// key signals a malformed or unexpected envelope, so callers route it
// to fallbackResultRender (dump the raw shape) rather than rendering a
// misleading "(0 rows)" line. A sentinel so callers can branch on it
// via errors.Is if they ever need to.
var errRowsKeyAbsent = errors.New("rows key absent from result envelope")

// decodeRowsResult decodes the canonical `{"rows": [...], "total": N}`
// envelope that every set-shaped gcloud read op returns. Returns the
// row list, or an error when the shape doesn't match.
//
// An absent `rows` key is treated as a malformed envelope and reported
// as errRowsKeyAbsent — distinct from a legitimately-empty list
// (`{"rows": []}`), which returns an empty slice and a nil error. A
// JSON null result (or no result at all) is the operation's "nothing to
// render" case and returns (nil, nil), unchanged from before.
func decodeRowsResult(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, fmt.Errorf("decode rows envelope: %w", err)
	}
	rowsRaw, ok := envelope["rows"]
	if !ok {
		return nil, errRowsKeyAbsent
	}
	var rows []map[string]any
	if err := json.Unmarshal(rowsRaw, &rows); err != nil {
		return nil, fmt.Errorf("decode rows: %w", err)
	}
	return rows, nil
}

// decodeFlatResult decodes a flat-dict result (gcloud.about,
// gcloud.project.describe, gcloud.iam.policy.read). Returns the
// decoded map or an error.
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

// stringField pulls a string field from a result entry, returning empty
// string when the field is missing or wrong type. Mirrors the bind9 /
// k8s siblings.
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

// boolField pulls a boolean field from a result entry, returning false
// when the field is missing or wrong type.
func boolField(e map[string]any, key string) bool {
	v, ok := e[key]
	if !ok {
		return false
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return false
}

// fallbackResultRender dumps the result envelope verbatim when the
// typed per-verb decode fails. Used by every verb's pretty-printer
// so contract drift surfaces with the same affordance.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := dispatch.PrettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}

// printErrorTrailer surfaces the dispatcher error / extras envelope.
// Mirrors the bind9 / k8s siblings.
func printErrorTrailer(w io.Writer, r *CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		pretty, err := dispatch.PrettyJSON(r.Extras)
		if err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}
