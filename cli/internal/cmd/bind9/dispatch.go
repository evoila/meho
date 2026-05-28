// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The verb files keep referring to the unqualified names; the operation-
// call logic lives once in the dispatch package. The bind9-specific
// result decoders + renderers below stay here.
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

// printErrorTrailer renders the connector error + extras tail shared by
// the bind9 per-verb pretty-printers (the success body is verb-specific).
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
// string when the field is missing or wrong type.
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
	pretty, err := dispatch.PrettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}
