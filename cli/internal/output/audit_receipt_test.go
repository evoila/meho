// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"net/http"
	"testing"
)

func TestAuditIDFromResponse(t *testing.T) {
	if got := AuditIDFromResponse(nil); got != nil {
		t.Errorf("nil response: got %v", *got)
	}
	r := &http.Response{Header: http.Header{}}
	if got := AuditIDFromResponse(r); got != nil {
		t.Errorf("no header: got %v", *got)
	}
	r.Header.Set("meho-audit-id", "abc")
	if got := AuditIDFromResponse(r); got == nil || *got != "abc" {
		t.Errorf("header set: got %v", got)
	}
}

func TestWriteAuditReceipt(t *testing.T) {
	var buf bytes.Buffer
	WriteAuditReceipt(&buf, nil)
	empty := ""
	WriteAuditReceipt(&buf, &empty)
	if buf.Len() != 0 {
		t.Errorf("nil or empty id should print nothing; got %q", buf.String())
	}
	id := "abc"
	WriteAuditReceipt(&buf, &id)
	if buf.String() != "audit receipt: abc\n" {
		t.Errorf("got %q", buf.String())
	}
}
