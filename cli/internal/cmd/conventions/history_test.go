// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"bytes"
	"testing"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

func TestPrintHistoryProvenance(t *testing.T) {
	kind := "updated"
	requestID := uuid.MustParse("33333333-3333-3333-3333-333333333333")
	auditID := uuid.MustParse("44444444-4444-4444-4444-444444444444")

	cases := []struct {
		name  string
		entry api.ConventionHistoryEntry
		want  string
	}{
		{
			name:  "new row with request id",
			entry: api.ConventionHistoryEntry{ChangeKind: &kind, RequestId: &requestID},
			want:  "    change=updated  request_id=33333333-3333-3333-3333-333333333333\n",
		},
		{
			name:  "new row without request id",
			entry: api.ConventionHistoryEntry{ChangeKind: &kind},
			want:  "    change=updated  request_id=<none>\n",
		},
		{
			name:  "legacy row",
			entry: api.ConventionHistoryEntry{AuditId: &auditID},
			want:  "    audit_id=44444444-4444-4444-4444-444444444444\n",
		},
		{
			name:  "seeded row",
			entry: api.ConventionHistoryEntry{},
			want:  "    audit_id=<seed>\n",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			printHistoryProvenance(&buf, tc.entry)
			if got := buf.String(); got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}
