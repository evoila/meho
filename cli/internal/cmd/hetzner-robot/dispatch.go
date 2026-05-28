// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package hetznerrobot

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// Pre-#1274 hetzner-robot carried its own dispatchOp / renderCallResult /
// printGenericResult trio because the shared dispatch.Connector hadn't
// owned the authed transport yet — the local copies threaded the per-
// dir doAuthedRequest into the request loop. G0.12-T16 #1274 promoted
// the transport into dispatch.Connector itself; hetzner-robot now folds
// onto the shared pattern (matching bind9 / k8s / vault / etc.).
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

// decodeRobotList decodes the result of a Robot list op. Hetzner Robot
// may return either a bare JSON array or a wrapper object with a named
// list key (e.g. {"server": [...]}).
func decodeRobotList(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	// Try bare array first (the most common shape for the curated ops).
	var arr []map[string]any
	if err := json.Unmarshal(raw, &arr); err == nil {
		return arr, nil
	}
	// Fall back to object: find the first array-valued key.
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, fmt.Errorf("decode robot list: not an array or object: %w", err)
	}
	for _, v := range obj {
		var inner []map[string]any
		if err := json.Unmarshal(v, &inner); err == nil {
			return inner, nil
		}
	}
	return nil, fmt.Errorf("decode robot list: no array found in object")
}
