// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/evoila/meho/cli/internal/output"
)

// scriptShownBelow replaces the script text in the printed preview JSON,
// because printScript prints the text line by line.
const scriptShownBelow = "(shown below under \"Script to run\")"

// scriptPreview returns the preview of a request whose preview carries a
// script (linux.script.run), or nil.
func scriptPreview(effect map[string]interface{}) map[string]interface{} {
	preview, ok := effect["preview"].(map[string]interface{})
	if !ok {
		return nil
	}
	_, hasScript := preview["script"].(string)
	_, unavailable := preview["script_unavailable"].(string)
	if !hasScript && !unavailable {
		return nil
	}
	return preview
}

// effectForPrint returns effect with the script text of its preview
// replaced by a pointer to the "Script to run" block. effect itself is
// not changed.
func effectForPrint(effect map[string]interface{}) map[string]interface{} {
	preview := scriptPreview(effect)
	if preview == nil {
		return effect
	}
	if _, ok := preview["script"].(string); !ok {
		return effect
	}
	shownPreview := make(map[string]interface{}, len(preview))
	for k, v := range preview {
		shownPreview[k] = v
	}
	shownPreview["script"] = scriptShownBelow
	shown := make(map[string]interface{}, len(effect))
	for k, v := range effect {
		shown[k] = v
	}
	shown["preview"] = shownPreview
	return shown
}

// printEffect prints the proposed effect as indented JSON under label, and
// then the script block when the preview carries a script.
func printEffect(w io.Writer, indent, label string, effect map[string]interface{}) {
	b, _ := json.MarshalIndent(effectForPrint(effect), indent+"  ", "  ")
	fmt.Fprintf(w, "%s%s\n%s  %s\n", indent, label, indent, output.SafeLines(string(b)))
	printScript(w, indent, effect)
}

// printScript prints the script of a script-run request: its size, its
// SHA-256, the reviewed copy and the hash check, then the text with "| "
// in front of each line. The text is made safe for the terminal line by
// line; a tab stays a tab.
func printScript(w io.Writer, indent string, effect map[string]interface{}) {
	preview := scriptPreview(effect)
	if preview == nil {
		return
	}
	fmt.Fprintf(w, "%sScript to run:\n", indent)
	if size, ok := preview["script_bytes"].(float64); ok {
		fmt.Fprintf(w, "%s  Size:             %d bytes\n", indent, int64(size))
	}
	for _, field := range []struct{ key, label string }{
		{"script_sha256", "SHA-256:         "},
		{"script_ref", "Reviewed copy:   "},
		{"script_check", "Expected SHA-256:"},
	} {
		if v, ok := preview[field.key].(string); ok && v != "" {
			fmt.Fprintf(w, "%s  %s %s\n", indent, field.label, output.SafeText(v))
		}
	}
	if hidden, _ := preview["script_values_hidden"].(bool); hidden {
		fmt.Fprintf(w, "%s  Values that look like secrets are hidden below. MEHO shows no SHA-256 for this script.\n", indent)
	}
	if cut, _ := preview["script_truncated"].(bool); cut {
		if sha, _ := preview["script_sha256"].(string); sha != "" {
			fmt.Fprintf(w, "%s  The script is longer than the text below. Compare its SHA-256 with a reviewed copy.\n", indent)
		} else {
			fmt.Fprintf(w, "%s  The script is longer than the text below. Read the full script in a reviewed copy before you approve.\n", indent)
		}
	}
	script, ok := preview["script"].(string)
	if !ok {
		reason, _ := preview["script_unavailable"].(string)
		fmt.Fprintf(w, "%s  %s\n", indent, output.SafeText(reason))
		return
	}
	for _, line := range strings.Split(strings.TrimSuffix(script, "\n"), "\n") {
		parts := strings.Split(line, "\t")
		for i, part := range parts {
			parts[i] = output.SafeText(part)
		}
		fmt.Fprintf(w, "%s  | %s\n", indent, strings.Join(parts, "\t"))
	}
}
