// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
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
	"delete preview failure": func(s string) string {
		var b bytes.Buffer
		cmd := &cobra.Command{}
		cmd.SetOut(&b)
		_ = renderDeletePreview(cmd, "keycloak.user.delete",
			&dispatch.PreviewResult{Status: "error", Error: &s, Extras: json.RawMessage(s)}, false)
		return b.String()
	},
	"delete preview failure with JSON extras": func(s string) string {
		var b bytes.Buffer
		cmd := &cobra.Command{}
		cmd.SetOut(&b)
		_ = renderDeletePreview(cmd, "keycloak.user.delete",
			&dispatch.PreviewResult{Status: "unavailable", Error: &s, Extras: json.RawMessage(`{"note":"x"}`)}, false)
		return b.String()
	},
	"fallback result": func(s string) string {
		var b bytes.Buffer
		fallbackResultRender(&b, &CallResult{Result: json.RawMessage(s)})
		return b.String()
	},
}

// TestRenderersShowServerControlCharactersAsEscapes — each renderer shows
// ESC and BEL in server text as visible escapes and sends no raw byte.
func TestRenderersShowServerControlCharactersAsEscapes(t *testing.T) {
	for name, render := range serverTextRenderers {
		t.Run(name, func(t *testing.T) {
			out := render(controlText)
			if !strings.Contains(out, `\x1b`) || !strings.Contains(out, `\x07`) {
				t.Errorf("output has no visible \\x1b or \\x07 escape:\n%q", out)
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
