// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package settings

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

// The rows below are the one reading of a diff. The console builds the
// same rows in backend/.../ui/routes/settings.py (diff_rows), and both
// are tested against testdata/preview_rows.json, so the CLI and the
// console show the same preview for the same file.

// diffItem is one entry of a section list. Tenants have no key; policy
// changes hold plain values; convention and target changes hold only
// the keys that change.
type diffItem struct {
	Tenant string `json:"tenant"`
	Key    string `json:"key"`
	Before any    `json:"before"`
	After  any    `json:"after"`
}

type diffSection struct {
	Added     []diffItem `json:"added"`
	Changed   []diffItem `json:"changed"`
	NotInFile []diffItem `json:"not_in_file"`
}

type diffBody struct {
	Tenants     diffSection `json:"tenants"`
	Policies    diffSection `json:"policies"`
	Conventions diffSection `json:"conventions"`
	Targets     diffSection `json:"targets"`
}

func (d *diffBody) sections() []struct {
	name string
	body diffSection
} {
	return []struct {
		name string
		body diffSection
	}{
		{"Tenants", d.Tenants},
		{"Policies", d.Policies},
		{"Conventions", d.Conventions},
		{"Targets", d.Targets},
	}
}

func (d *diffBody) hasChanges() bool {
	for _, s := range d.sections() {
		if len(s.body.Added) > 0 || len(s.body.Changed) > 0 {
			return true
		}
	}
	return false
}

// diffRow is one line of the rendered diff.
type diffRow struct {
	Section string `json:"section"`
	Kind    string `json:"kind"`
	Tenant  string `json:"tenant"`
	Key     string `json:"key"`
	Detail  string `json:"detail"`
}

func diffRows(d *diffBody) []diffRow {
	rows := []diffRow{}
	for _, s := range d.sections() {
		for _, item := range s.body.Added {
			rows = append(rows, diffRow{s.name, "added", item.Tenant, item.Key, ""})
		}
		for _, item := range s.body.Changed {
			rows = append(rows, diffRow{s.name, "changed", item.Tenant, item.Key, changeDetail(item)})
		}
		for _, item := range s.body.NotInFile {
			rows = append(rows, diffRow{s.name, "not in file", item.Tenant, item.Key, ""})
		}
	}
	return rows
}

// changeDetail is `a -> b` for a policy and `key: a -> b; ...`, in key
// order, for an item.
func changeDetail(item diffItem) string {
	before, ok := item.Before.(map[string]any)
	after, ok2 := item.After.(map[string]any)
	if !ok || !ok2 {
		return compact(item.Before) + " -> " + compact(item.After)
	}
	keys := make([]string, 0, len(after))
	for key := range after {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, key+": "+compact(before[key])+" -> "+compact(after[key]))
	}
	return strings.Join(parts, "; ")
}

// compact is one value as compact JSON with sorted keys.
func compact(value any) string {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return fmt.Sprint(value)
	}
	return strings.TrimSpace(buf.String())
}

func printPreview(w io.Writer, answer *importAnswer) {
	printSentences(w, "Mistakes", answer.Mistakes)
	printSentences(w, "Warnings", answer.Warnings)
	if answer.Diff == nil {
		return
	}
	printDiff(w, answer.Diff)
	if !answer.Diff.hasChanges() {
		fmt.Fprintln(w, "Nothing to change.")
	}
}

func printSentences(w io.Writer, title string, sentences []string) {
	if len(sentences) == 0 {
		return
	}
	fmt.Fprintf(w, "%s:\n", title)
	for _, sentence := range sentences {
		fmt.Fprintf(w, "  - %s\n", sentence)
	}
	fmt.Fprintln(w)
}

func printDiff(w io.Writer, d *diffBody) {
	section := ""
	notInFile := false
	for _, row := range diffRows(d) {
		if row.Section != section {
			section = row.Section
			fmt.Fprintf(w, "%s:\n", section)
		}
		notInFile = notInFile || row.Kind == "not in file"
		line := fmt.Sprintf("  %-11s  %s", row.Kind, row.Tenant)
		if row.Key != "" {
			line += "  " + row.Key
		}
		if row.Detail != "" {
			line += "  " + row.Detail
		}
		fmt.Fprintln(w, line)
	}
	if notInFile {
		fmt.Fprintln(w, "\n`not in file` is information only: an import never deletes.")
	}
}

func printApply(w io.Writer, answer *importAnswer) {
	printSentences(w, "Could not be written", answer.Failed)
	switch {
	case answer.RevisionID != nil:
		fmt.Fprintf(w, "\nApplied; revision %s.\n", *answer.RevisionID)
	case answer.Applied:
		fmt.Fprintln(w, "\nApplied.")
	default:
		fmt.Fprintln(w, "\nNothing was changed.")
	}
}
