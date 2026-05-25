// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
)

// runEditor opens the operator's preferred editor on a tempfile seeded
// with `initialContent` and returns the post-save contents. The
// editor's exit code is propagated as an error (non-zero exit aborts
// the edit verb with no API call). The tempfile is removed on return.
//
// Editor precedence follows the issue body's contract: $EDITOR first,
// then $VISUAL, then `vi`. (Standard POSIX is VISUAL first then
// EDITOR; many CLIs — git, crontab — invert this. The issue body
// pinned the order explicitly; we honour it.) The editor value is
// word-split on whitespace so values like `vim -u NORC` or
// `code --wait` work; we deliberately do NOT route through a shell
// (no `sh -c`) so an editor value containing shell metacharacters
// can't smuggle injection.
//
// The tempfile carries a `.md` suffix so editors that auto-pick syntax
// highlighting (vim, emacs, code) render Markdown by default.
//
// `runEditor` is a package-level var so unit tests can stub the editor
// integration without spawning a real editor — see crud_test.go.
var runEditor = func(cmd *cobra.Command, initialContent string) (string, error) {
	editor := os.Getenv("EDITOR")
	if editor == "" {
		editor = os.Getenv("VISUAL")
	}
	if editor == "" {
		editor = "vi"
	}

	tmp, err := os.CreateTemp("", "meho-conv-*.md")
	if err != nil {
		return "", fmt.Errorf("create tempfile for editor: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	if _, err := tmp.WriteString(initialContent); err != nil {
		tmp.Close()
		return "", fmt.Errorf("seed tempfile: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return "", fmt.Errorf("close tempfile: %w", err)
	}

	// Split the editor value on whitespace so `EDITOR="vim -u NORC"`
	// invokes vim with the flag, not a binary literally named
	// "vim -u NORC". No shell expansion (no sh -c) — the goal is to
	// support the realistic editor-with-flags case, not to evaluate
	// arbitrary shell.
	parts := strings.Fields(editor)
	if len(parts) == 0 {
		return "", fmt.Errorf("editor command is empty after parsing %q", editor)
	}
	args := append(parts[1:], tmpPath)

	editorCmd := exec.CommandContext(cmd.Context(), parts[0], args...) //nolint:gosec // operator-supplied editor; documented contract
	editorCmd.Stdin = os.Stdin
	editorCmd.Stdout = os.Stdout
	editorCmd.Stderr = os.Stderr
	if err := editorCmd.Run(); err != nil {
		return "", fmt.Errorf("editor %q exited with error: %w", editor, err)
	}

	data, err := os.ReadFile(tmpPath)
	if err != nil {
		return "", fmt.Errorf("read edited tempfile: %w", err)
	}
	return string(data), nil
}
