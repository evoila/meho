// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// Pre-#1274 pfsense carried its own dispatchOp / renderCallResult /
// printGenericResult trio because the shared dispatch.Connector hadn't
// owned the authed transport yet — the local copies threaded the per-
// dir doAuthedRequest into the request loop. G0.12-T16 #1274 promoted
// the transport into dispatch.Connector itself; pfsense now folds onto
// the shared pattern (matching bind9 / k8s / vault / etc.).
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
// files continue calling the unqualified name they were authored
// against.
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

// printErrorTrailer surfaces the dispatcher error + extras envelope.
// Used by the per-verb pretty-printers' non-ok branch.
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
// envelope every set-shaped pfSense read op returns (firewall.rules,
// firewall.state, nat.rules, interface.list, gateway.list).
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

// decodeFlatResult decodes a flat-dict result (pfsense.about,
// pfsense.version, pfsense.config.show).
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
