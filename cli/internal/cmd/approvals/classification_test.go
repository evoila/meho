// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"
)

func TestPrintDetailShowsTheClassification(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	d.ProposedEffect = map[string]interface{}{
		"safety_level":        "caution",
		"category":            "network",
		"labels":              map[string]interface{}{"team": "net", "env": "prod"},
		"classification_rule": "tenant:classifications[0]",
	}
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	out := stdout.String()
	for _, want := range []string{
		"Category:     network\n",
		"Labels:       env=prod, team=net\n",
		"Rule:         tenant:classifications[0]\n",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDetail output misses %q in:\n%s", want, out)
		}
	}
}

func TestPrintClassificationNamesAConflict(t *testing.T) {
	var out bytes.Buffer
	printClassification(&out, map[string]interface{}{
		"category":            nil,
		"labels":              map[string]interface{}{},
		"classification_rule": "tenant:classifications[0]",
	})
	want := "Category:     none (two classification rules disagree)\n" +
		"Rule:         tenant:classifications[0]\n"
	if out.String() != want {
		t.Errorf("got %q, want %q", out.String(), want)
	}
}

func TestPrintClassificationPrintsNothingWithoutIt(t *testing.T) {
	for _, effect := range []map[string]interface{}{
		nil,
		{"safety_level": "caution", "op_class": "write"},
	} {
		var out bytes.Buffer
		printClassification(&out, effect)
		if out.Len() != 0 {
			t.Errorf("printClassification(%v) printed %q, want nothing", effect, out.String())
		}
	}
}

func TestPrintClassificationEscapesServerText(t *testing.T) {
	var out bytes.Buffer
	printClassification(&out, map[string]interface{}{
		"category":            "net\x1b[2K",
		"labels":              map[string]interface{}{"team": "a\rb"},
		"classification_rule": "rule\a",
	})
	assertNoRawControls(t, out.String())
}
