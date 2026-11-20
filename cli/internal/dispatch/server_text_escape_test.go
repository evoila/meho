// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// controlText holds an "erase line" sequence and a BEL. plainText holds
// letters outside ASCII.
const (
	controlText = "a\x1b[2Kb\a"
	plainText   = "Čačak日本"
)

// serverTextRenderers prints server text through each human renderer of
// this package. Each entry puts s in the server fields it prints.
var serverTextRenderers = map[string]func(s string) string{
	"error trailer": func(s string) string {
		var b bytes.Buffer
		WriteErrorTrailer(&b, &CallResult{Status: "error", Error: &s, Extras: json.RawMessage(s)})
		return b.String()
	},
	"error trailer without error text": func(s string) string {
		var b bytes.Buffer
		WriteErrorTrailer(&b, &CallResult{Status: s})
		return b.String()
	},
	"not found ids": func(s string) string {
		var b bytes.Buffer
		extras, _ := json.Marshal(map[string]any{"resource_ids": []string{s, s}})
		WriteErrorTrailer(&b, &CallResult{Status: StatusNotFound, Extras: extras})
		// The extras block is JSON, which encodes the control bytes on
		// its own; keep only the "not found" line.
		for _, line := range strings.Split(b.String(), "\n") {
			if strings.HasPrefix(line, "not found:") {
				return line
			}
		}
		return ""
	},
	"parked approval id": func(s string) string {
		var b bytes.Buffer
		extras, _ := json.Marshal(map[string]any{"approval_request_id": s})
		WriteParkedHint(&b, extras)
		return b.String()
	},
	"preview failure": func(s string) string {
		var b bytes.Buffer
		cmd := &cobra.Command{}
		cmd.SetOut(&b)
		_ = New("c").RenderPreviewFailure(cmd, "x.delete",
			&PreviewResult{Status: "error", Error: &s, Extras: json.RawMessage(s)}, false)
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
