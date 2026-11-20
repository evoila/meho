// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestPrintConventionBodyEscapesControlCharacters — `meho conventions
// show` prints a body with control characters as visible escapes and
// keeps its line breaks.
func TestPrintConventionBodyEscapesControlCharacters(t *testing.T) {
	var buf bytes.Buffer
	printConventionBody(&buf, &api.Convention{Body: "rule one\x1b[2K\r\nrule two\x07\n"})
	want := "rule one\\x1b[2K\nrule two\\x07\n"
	if got := buf.String(); got != want {
		t.Errorf("unexpected render: got %q; want %q", got, want)
	}
}

// TestPrintHistoryDiffsEscapesControlCharacters — `meho conventions
// history` escapes the actor and every body line of the diff.
func TestPrintHistoryDiffsEscapesControlCharacters(t *testing.T) {
	oldBody := "keep\nold\x1b[2K"
	var buf bytes.Buffer
	printHistoryDiffs(&buf, "slug", []api.ConventionHistoryEntry{
		{ActorSub: "bob\x07", BodyAfter: "first\x1b]0;t\x07"},
		{ActorSub: "bob", BodyBefore: &oldBody, BodyAfter: "keep\nne\rw"},
	})
	got := buf.String()
	if strings.ContainsAny(got, "\x1b\x07\r") {
		t.Fatalf("raw control character in output: %q", got)
	}
	for _, want := range []string{`actor=bob\x07`, `+ first\x1b]0;t\x07`, `- old\x1b[2K`, `+ ne\x0dw`, "  keep\n"} {
		if !strings.Contains(got, want) {
			t.Errorf("output %q missing %q", got, want)
		}
	}
}
