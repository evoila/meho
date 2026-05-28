// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfautomation

import (
	"context"
	"encoding/json"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// vcf-automation keeps its own dispatchOp wrapper because it threads a
// per-call "fqdn" vhost override into the target dict (G3.6-T12 #840) —
// the wrapper delegates to the shared Connector.CallWithTarget.
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

// dispatchOp POSTs an OperationCall with connector_id="vcfa-rest-9.0"
// pre-baked. The optional fqdn is threaded into the request body's
// target dict as the "fqdn" field — the backend honours it as a
// per-call vhost override on the resolved Target row (G3.6-T12 #840).
// Empty fqdn is omitted so the backend reads target.fqdn from the
// registry; an empty targetSlug omits the target entirely.
func dispatchOp(
	ctx context.Context,
	backplaneURL, opID, targetSlug, fqdn string,
	params map[string]any,
) (*CallResult, error) {
	var target map[string]any
	if targetSlug != "" {
		target = map[string]any{"name": targetSlug}
		if fqdn != "" {
			target["fqdn"] = fqdn
		}
	}
	return conn.CallWithTarget(ctx, backplaneURL, opID, target, params)
}

// jsonUnmarshalStrict decodes raw into out. Thin wrapper retained so the
// per-verb decoders read uniformly.
func jsonUnmarshalStrict(raw []byte, out any) error {
	return json.Unmarshal(raw, out)
}
