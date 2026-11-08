// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"encoding/json"
	"fmt"
	"io"
)

// ResultHandleID returns the handle_id of a result handle, or "" when there is none.
func ResultHandleID(raw json.RawMessage) string {
	var h struct {
		HandleID string `json:"handle_id"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &h) != nil {
		return ""
	}
	return h.HandleID
}

// WriteResultHandle writes the lines that tell the operator how to read
// a reduced result: the handle id and the `meho operation result-query`
// command that reads the full result. When the backplane could not hand
// over the whole result (delivery partial or unavailable), it also says
// that the operation already ran, so the operator does not run it again.
// It writes nothing when the envelope has no handle and the delivery is
// complete or not set.
func WriteResultHandle(w io.Writer, handle json.RawMessage, delivery *string) {
	if id := ResultHandleID(handle); id != "" {
		fmt.Fprintf(w, "result handle: %s\n", id)
		fmt.Fprintf(w, "  the result above is reduced; read all of it with: meho operation result-query %s\n", id)
	}
	if delivery != nil && (*delivery == "partial" || *delivery == "unavailable") {
		fmt.Fprintf(w, "delivery: %s\n", *delivery)
		fmt.Fprintln(w, "delivery incomplete: operation already executed; do not re-invoke it.")
	}
}
