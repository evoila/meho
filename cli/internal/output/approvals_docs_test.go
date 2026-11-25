// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestApprovalsAdminTextInDocs checks that the examples of the Admin
// approvals line in the docs hold the text of approvalsAdmin word for
// word. Change the text in format.go and in both files together.
func TestApprovalsAdminTextInDocs(t *testing.T) {
	files := []string{
		"docs/codebase/cli.md",
		"docs-site/install/form-3-vks.md",
	}
	for _, rel := range files {
		t.Run(rel, func(t *testing.T) {
			data, err := os.ReadFile(filepath.Join("..", "..", "..", filepath.FromSlash(rel)))
			if err != nil {
				t.Fatalf("read %s: %v", rel, err)
			}
			if !strings.Contains(string(data), approvalsAdmin) {
				t.Errorf("%s does not hold the text of approvalsAdmin word for word:\n%s", rel, approvalsAdmin)
			}
		})
	}
}
