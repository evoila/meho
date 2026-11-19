// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
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
	"search table": func(s string) string {
		var b bytes.Buffer
		printSearchTable(&b, "c", "q", &SearchResponse{
			Hits:          []SearchHit{{OpID: s, SafetyLevel: s, Summary: &s}},
			RawOperations: &RawOperations{Note: &s},
		}, false)
		return b.String()
	},
	"search table with scores": func(s string) string {
		var b bytes.Buffer
		printSearchTable(&b, "c", "q", &SearchResponse{
			Hits: []SearchHit{{OpID: s, Summary: &s, FusedScore: 0.5}},
		}, true)
		return b.String()
	},
	"operation contract": func(s string) string {
		var b bytes.Buffer
		quoted, _ := json.Marshal(s)
		printContract(&b, &Contract{
			OpID: s, ConnectorID: s, Summary: &s, SafetyLevel: s,
			Examples:    []ContractExample{{Param: &s, Value: quoted}},
			Target:      ContractTarget{Guidance: s},
			ResultShape: ContractResult{Guidance: s},
			Recovery:    []string{s},
		})
		return b.String()
	},
	"groups table": func(s string) string {
		var b bytes.Buffer
		ops := []GroupOperation{{OpID: s, SafetyLevel: s, Summary: &s}}
		printGroupsTable(&b, &GroupsResponse{
			ConnectorID: s,
			Groups: []GroupSummary{{
				GroupKey: s, Name: s, WhenToUse: s,
				Operations: &ops, OperationsMore: 1,
			}},
			NextCursor:    &s,
			RawOperations: &RawOperations{Note: &s},
			Note:          s,
		})
		return b.String()
	},
	"call failure": func(s string) string {
		var b bytes.Buffer
		extras, _ := json.Marshal(map[string]any{"resource_ids": []string{s}})
		PrintCallResult(&b, "c", "op", &CallResult{
			Status: "not_found", Error: &s, Extras: extras,
		})
		return b.String()
	},
	"preview failure": func(s string) string {
		var b bytes.Buffer
		printPreviewResult(&b, "c", "op", &PreviewResult{
			Status: s, SourceKind: s, SafetyLevel: s, Error: &s,
		})
		return b.String()
	},
	"preview": func(s string) string {
		var b bytes.Buffer
		shown := false
		printPreviewResult(&b, "c", "op", &PreviewResult{
			Status: "ok", Method: s, ResolvedPath: s, PreviewHash: s,
			PlanShown: &shown, PlanNote: s,
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
