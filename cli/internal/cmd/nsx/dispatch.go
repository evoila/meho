// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package nsx

import "github.com/evoila/meho/cli/internal/dispatch"

// Aliases + binding to the shared dispatch core (cli/internal/dispatch).
// The verb files in this package keep referring to the unqualified names;
// the operation-call logic lives once in the dispatch package.
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
