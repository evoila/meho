// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

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
	"describe": func(s string) string {
		var b bytes.Buffer
		fieldMap := map[string]string{"user": s}
		printTargetSummary(&b, &api.Target{
			Name: s, Aliases: []string{s}, Product: s, Host: s,
			Fqdn: &s, SecretRef: &s, SecretFieldMap: &fieldMap,
			PreferredImplId: &s, Notes: &s,
			Fingerprint: &map[string]interface{}{"product": s, "version": s, "probe_method": s},
			Extras:      map[string]interface{}{"note": s},
		})
		return b.String()
	},
	"describe notes only": func(s string) string {
		var b bytes.Buffer
		printTargetSummary(&b, &api.Target{Name: "t", Notes: &s})
		return b.String()
	},
	"describe probed version only": func(s string) string {
		var b bytes.Buffer
		printTargetSummary(&b, &api.Target{
			Name: "t", Fingerprint: &map[string]interface{}{"version": s},
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
