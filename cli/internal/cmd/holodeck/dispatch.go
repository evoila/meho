// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package holodeck

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The verb files in this package keep referring to the unqualified names;
// the operation-call logic lives once in the dispatch package.
//
// Pre-#1274 holodeck carried its own dispatchOp / renderCallResult /
// printGenericResult trio because the shared dispatch.Connector hadn't
// owned the authed transport yet — the local copies threaded the per-
// dir doAuthedRequest into the request loop. G0.12-T16 #1274 promoted
// the transport into dispatch.Connector itself; holodeck now folds onto
// the shared pattern (matching bind9 / k8s / vault / etc.).
//
// NOTE: holodeck.k8s.exec passes the operator's `kubectl` command
// verbatim in `params["command"]`. The CLI does not pre-parse or
// pre-validate that string — the read-only safelist + shell-
// metacharacter guard live on the backend handler. Any operator
// invocation containing `;` / `&&` / `|` / `$(...)` / backticks /
// `>` / `<` / newline is refused with `result_connector_error` and
// surfaces as a non-ok status on the CLI side.
type (
	// CallResult is the decoded OperationResult envelope.
	CallResult = dispatch.CallResult
	// callRequestBody is the on-the-wire OperationCall body (asserted by tests).
	callRequestBody = dispatch.CallRequestBody
)

// conn binds this package's pre-baked connector_id to the shared
// dispatch core. The authed transport (lazy *api.AuthedClient over the
// generated typed surface) lives inside dispatch.Connector after
// G0.12-T16 #1274 promoted the per-vendor doAuthedRequest copies.
var conn = dispatch.New(ConnectorID)

// dispatchOp is a thin wrapper around conn.Call kept so the per-verb
// files (about.go / pod.go / service.go / ...) continue calling the
// unqualified name they were authored against.
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
