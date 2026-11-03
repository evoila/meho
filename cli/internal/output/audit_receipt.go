// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"fmt"
	"io"
	"net/http"
)

// AuditIDHeader is the response header in which the backplane sends the
// id of the audit record of a request (#4976). Write routes that answer
// 204 No Content have no body, so this header is the only place the id
// can come back.
const AuditIDHeader = "Meho-Audit-Id"

// AuditIDFromResponse returns the audit id the backplane sent in the
// Meho-Audit-Id header, or nil when the response is nil or has no such
// header (an older backplane, or a request with no audit record).
func AuditIDFromResponse(r *http.Response) *string {
	if r == nil {
		return nil
	}
	id := r.Header.Get(AuditIDHeader)
	if id == "" {
		return nil
	}
	return &id
}

// WriteAuditReceipt writes the `audit receipt: <id>` line, or nothing
// when auditID is nil or empty. Every CLI write prints its receipt
// through this function, so the line is the same everywhere.
func WriteAuditReceipt(w io.Writer, auditID *string) {
	if auditID == nil || *auditID == "" {
		return
	}
	fmt.Fprintf(w, "audit receipt: %s\n", *auditID)
}
