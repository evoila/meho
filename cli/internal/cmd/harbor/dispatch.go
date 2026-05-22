// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"encoding/json"
	"fmt"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The verb files keep referring to the unqualified names; the operation-
// call logic lives once in the dispatch package.
type (
	// CallResult is the decoded OperationResult envelope.
	CallResult = dispatch.CallResult
	// callRequestBody is the on-the-wire OperationCall body (asserted by tests).
	callRequestBody = dispatch.CallRequestBody
)

// errOpError is the structured-failure sentinel (status error/denied).
var errOpError = dispatch.ErrOpError

// conn binds this package's pre-baked connector_id + authed transport
// (doAuthedRequest) to the shared dispatch core.
var conn = dispatch.Connector{ID: ConnectorID, Request: doAuthedRequest}

// decodeHarborList decodes the result of a Harbor list op. Harbor
// list endpoints return a bare JSON array (no "results" wrapper).
func decodeHarborList(raw json.RawMessage) ([]map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var items []map[string]any
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, fmt.Errorf("decode harbor list: %w", err)
	}
	return items, nil
}
