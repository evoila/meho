// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// controlText holds an "erase line" sequence and a BEL. plainText holds
// letters outside ASCII. Both are short, so no column cuts them.
const (
	controlText = "a\x1b[2Kb\a"
	plainText   = "Čačak日本"
)

// serverTextRenderers prints server text through each human renderer of
// this package. Each entry puts s in the server fields it prints.
var serverTextRenderers = map[string]func(s string) string{
	"trace list": func(s string) string {
		var b bytes.Buffer
		printTraceListTable(&b, &api.AuditQueryResult{
			Rows: []api.AuditEntry{{
				OpId: s, ResultStatus: s, TargetName: &s, TenantName: &s,
			}},
			NextCursor: &s,
		})
		return b.String()
	},
	"trace list with a long op id": func(s string) string {
		var b bytes.Buffer
		printTraceListTable(&b, &api.AuditQueryResult{
			Rows: []api.AuditEntry{{OpId: s + strings.Repeat("x", 40), ResultStatus: "ok"}},
		})
		return b.String()
	},
}

// TestRenderersShowServerControlCharactersAsEscapes — each renderer shows
// ESC and BEL in server text as visible escapes and sends no raw byte.
func TestRenderersShowServerControlCharactersAsEscapes(t *testing.T) {
	for name, render := range serverTextRenderers {
		t.Run(name, func(t *testing.T) {
			out := render(controlText)
			if !strings.Contains(out, `\x1b`) {
				t.Errorf("output has no visible \\x1b escape:\n%q", out)
			}
			if strings.ContainsAny(out, "\x1b\a") {
				t.Errorf("output holds a raw ESC or BEL byte:\n%q", out)
			}
		})
	}
}

// TestRenderersPrintPlainServerTextUnchanged — text without control
// characters, also letters outside ASCII, is printed as it is.
func TestRenderersPrintPlainServerTextUnchanged(t *testing.T) {
	for name, render := range serverTextRenderers {
		t.Run(name, func(t *testing.T) {
			if out := render(plainText); !strings.Contains(out, plainText) {
				t.Errorf("output does not hold %q unchanged:\n%s", plainText, out)
			}
		})
	}
}
