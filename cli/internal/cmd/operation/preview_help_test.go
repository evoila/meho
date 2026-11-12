// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"strings"
	"testing"
)

// TestPreviewHelpSaysWhichAuditRowIsWritten — the help must not say that a
// preview writes no audit row at all. The operation writes none, but the
// HTTP request writes one request row by default.
func TestPreviewHelpSaysWhichAuditRowIsWritten(t *testing.T) {
	long := newPreviewCmd().Long
	for _, want := range []string{
		"no audit row is written for it",
		"http.post:/api/v1/operations/preview",
		"AUDIT_WRAPPER_ROWS_RETIRED=true",
	} {
		if !strings.Contains(long, want) {
			t.Errorf("preview help must contain %q; got:\n%s", want, long)
		}
	}
	if strings.Contains(long, "Nothing is sent and no audit row is written.") {
		t.Errorf("preview help still says no audit row is written at all:\n%s", long)
	}
}
