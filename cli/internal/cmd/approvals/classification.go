// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/evoila/meho/cli/internal/output"
)

// printClassification prints the category, the labels and the
// classification rule a request carries in its proposed effect. The
// backplane adds them only while its POLICY_CLASSIFICATION switch is on,
// so an older request, or one made with the switch off, prints nothing.
// A null category means two classification rules disagree.
func printClassification(w io.Writer, effect map[string]interface{}) {
	if effect == nil {
		return
	}
	rule, hasRule := effect["classification_rule"]
	if !hasRule {
		return
	}
	if category, _ := effect["category"].(string); category != "" {
		fmt.Fprintf(w, "Category:     %s\n", output.SafeText(category))
	} else {
		fmt.Fprintln(w, "Category:     none (two classification rules disagree)")
	}
	if labels := labelText(effect["labels"]); labels != "" {
		fmt.Fprintf(w, "Labels:       %s\n", output.SafeText(labels))
	}
	if text, _ := rule.(string); text != "" {
		fmt.Fprintf(w, "Rule:         %s\n", output.SafeText(text))
	}
}

// labelText joins the labels as key=value, sorted by key. Empty when
// there are none or the value is not a JSON object.
func labelText(raw interface{}) string {
	labels, ok := raw.(map[string]interface{})
	if !ok || len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for key := range labels {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, fmt.Sprintf("%s=%v", key, labels[key]))
	}
	return strings.Join(parts, ", ")
}
