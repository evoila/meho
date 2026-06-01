// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package argocd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The argocd verb tree is authored against the shared dispatch.Connector
// pattern G0.12-T16 #1274 established (matching keycloak / pfsense /
// gcloud / harbor / nsx): the authed transport (lazy *api.AuthedClient
// over the generated typed surface) lives inside dispatch.Connector
// itself.
type (
	// CallResult is the decoded OperationResult envelope.
	CallResult = dispatch.CallResult
	// callRequestBody is the on-the-wire OperationCall body (asserted by tests).
	callRequestBody = dispatch.CallRequestBody
)

// conn binds this package's pre-baked connector_id to the shared
// dispatch core.
var conn = dispatch.New(ConnectorID)

// dispatchOp is a thin wrapper around conn.Call kept so the per-verb
// files call the unqualified name they were authored against.
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

// fallbackResultRender dumps the result envelope verbatim when the typed
// per-verb decode fails.
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

// decodeItemsResult decodes the canonical `{"items": [...], ...}`
// envelope the ArgoCD set-shaped read ops return (app.list,
// appproject.list, repo.list — all GET /api/v1/{applications,projects,
// repositories} list responses). `items` is nullable per the op
// response_schema, so a null/absent items field yields a nil slice with
// no error.
func decodeItemsResult(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var envelope struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return nil, fmt.Errorf("decode items: %w", err)
	}
	return envelope.Items, nil
}

// decodeObject decodes a bare object result (argocd.app.get returns the
// Application object verbatim: {metadata, spec, status}). Returns nil
// when the result is empty/null.
func decodeObject(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var obj map[string]any
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, fmt.Errorf("decode object: %w", err)
	}
	return obj, nil
}

// nestedString walks a chain of map keys and returns the string at the
// leaf, or "" when any hop is missing or the leaf is not a string. Used
// to pull status.sync.status / status.health.status out of an
// Application without a per-level nil dance at every call site.
func nestedString(obj map[string]any, keys ...string) string {
	cur := obj
	for i, k := range keys {
		v, ok := cur[k]
		if !ok || v == nil {
			return ""
		}
		if i == len(keys)-1 {
			if s, ok := v.(string); ok {
				return s
			}
			return ""
		}
		next, ok := v.(map[string]any)
		if !ok {
			return ""
		}
		cur = next
	}
	return ""
}
