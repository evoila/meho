// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package main

import (
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/cmd"
)

func TestRenderCommandPrintsTheReferenceNoteUnderTheShortHelp(t *testing.T) {
	c := &cobra.Command{
		Use:         "explain",
		Short:       "One short sentence.",
		Annotations: map[string]string{referenceNoteKey: "A note for the reference."},
		Run:         func(*cobra.Command, []string) {},
	}
	var sb strings.Builder
	renderCommand(&sb, c, 0)
	want := "One short sentence.\n\nA note for the reference.\n\n```"
	if !strings.Contains(sb.String(), want) {
		t.Fatalf("note not printed under the short help:\n%s", sb.String())
	}
}

func TestPolicyExplainReferenceSaysApproverGroupsAreMehoGroupKinds(t *testing.T) {
	page := render(cmd.NewRootCmdForDocs())
	start := strings.Index(page, "### `meho policy explain`")
	if start < 0 {
		t.Fatal("no section for meho policy explain")
	}
	section := page[start:]
	section = section[:strings.Index(section[3:], "\n### ")+3]
	if !strings.Contains(section, "MEHO group kinds (MEHO-<kind>), not the names of the login groups of your install") {
		t.Fatalf("section lacks the group kinds note:\n%s", section)
	}
}
