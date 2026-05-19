// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// MemoryFile holds the parsed representation of a single Claude Code
// memory entry (a *.md file in the laptop-local memory directory).
// Fields are exported so callers (T4 picker, T5 submit client) can
// access them directly.
type MemoryFile struct {
	// Path is the absolute path to the source file.
	Path string

	// Name is the `name:` frontmatter field, or empty when
	// FrontmatterOK is false.
	Name string

	// Description is the `description:` frontmatter field.
	Description string

	// Type is the `type:` frontmatter field (e.g. "user", "project",
	// "reference", "feedback"). Empty when FrontmatterOK is false.
	Type string

	// Metadata holds the raw `metadata:` frontmatter value as a
	// passthrough map; callers that need it unmarshal from this map.
	Metadata map[string]any

	// Body is the post-frontmatter content. When FrontmatterOK is
	// false, Body holds the entire file content.
	Body string

	// BodySHA256 is the lowercase hex SHA-256 of the Body bytes. Used
	// by T5 as the idempotency source_id; stable across calls and
	// changes whenever Body changes.
	BodySHA256 string

	// FrontmatterOK is true iff the file began with a well-formed
	// YAML frontmatter block delimited by `---` lines. A file with no
	// frontmatter or malformed YAML sets this to false without causing
	// an error return.
	FrontmatterOK bool

	// ParseWarning carries a human-readable description of the
	// parsing problem when FrontmatterOK is false. Empty when
	// FrontmatterOK is true.
	ParseWarning string

	// MachineLocalOptOut is true iff the Body contains the literal
	// HTML comment `<!-- meho:machine-local -->` (case-sensitive;
	// whitespace-tolerant inside the delimiters). Operators place this
	// marker to explicitly exclude a file from cross-machine migration.
	MachineLocalOptOut bool
}

// ResolveSourceDir resolves the memory directory to scan.
//
// Resolution order (first non-empty wins):
//  1. override — returned verbatim when non-empty.
//  2. $CLAUDE_PROJECT_DIR/memory/ — when that env var is set.
//  3. <os.UserHomeDir()>/.claude/projects/<sanitized-abs-cwd>/memory/
//
// The sanitized-abs-cwd transform replaces every '/' in the absolute
// working directory with '-', so the leading '/' becomes a leading '-'
// (e.g. "/Users/x/repos/meho" → "-Users-x-repos-meho"). The leading
// separator is retained. This mirrors the layout Claude Code uses when
// writing project-scoped memory files under $HOME/.claude/projects/.
func ResolveSourceDir(override string) (string, error) {
	if override != "" {
		return override, nil
	}

	if projectDir := os.Getenv("CLAUDE_PROJECT_DIR"); projectDir != "" {
		return filepath.Join(projectDir, "memory"), nil
	}

	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve home dir: %w", err)
	}

	cwd, err := os.Getwd()
	if err != nil {
		return "", fmt.Errorf("resolve cwd: %w", err)
	}

	sanitized := strings.ReplaceAll(cwd, "/", "-")
	return filepath.Join(home, ".claude", "projects", sanitized, "memory"), nil
}

// ScanDir walks dir for *.md files (non-recursive; the memory directory
// is flat) and returns a MemoryFile for each. A file with no or malformed
// frontmatter is not an error: its MemoryFile has FrontmatterOK=false,
// Body set to the entire file content, and ParseWarning describing the
// issue.
//
// Returns an error only when dir is unreadable.
func ScanDir(dir string) ([]MemoryFile, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("scan dir %q: %w", dir, err)
	}

	var files []MemoryFile
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		if !strings.HasSuffix(entry.Name(), ".md") {
			continue
		}

		path := filepath.Join(dir, entry.Name())
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %q: %w", path, err)
		}

		mf := parseMemoryFile(path, raw)
		files = append(files, mf)
	}
	return files, nil
}

// parseMemoryFile parses raw file content into a MemoryFile. It is
// exported only to the test file via the package boundary; callers
// outside the package use ScanDir.
func parseMemoryFile(path string, raw []byte) MemoryFile {
	mf := MemoryFile{Path: path}

	fm, body, ok, warn := splitFrontmatter(raw)
	if !ok {
		mf.FrontmatterOK = false
		mf.ParseWarning = warn
		mf.Body = string(raw)
		mf.BodySHA256 = sha256Hex(raw)
		mf.MachineLocalOptOut = hasMachineLocalComment(string(raw))
		return mf
	}

	var header struct {
		Name        string         `yaml:"name"`
		Description string         `yaml:"description"`
		Type        string         `yaml:"type"`
		Metadata    map[string]any `yaml:"metadata"`
	}

	if err := yaml.Unmarshal(fm, &header); err != nil {
		mf.FrontmatterOK = false
		mf.ParseWarning = fmt.Sprintf("frontmatter YAML parse error: %v", err)
		mf.Body = string(raw)
		mf.BodySHA256 = sha256Hex(raw)
		mf.MachineLocalOptOut = hasMachineLocalComment(string(raw))
		return mf
	}

	mf.FrontmatterOK = true
	mf.Name = header.Name
	mf.Description = header.Description
	mf.Type = header.Type
	mf.Metadata = header.Metadata
	mf.Body = string(body)
	mf.BodySHA256 = sha256Hex(body)
	mf.MachineLocalOptOut = hasMachineLocalComment(string(body))
	return mf
}

// splitFrontmatter splits raw file bytes into (frontmatter, body, ok,
// warning). A file without a leading `---\n` returns ok=false and the
// caller treats the whole file as body. An unterminated frontmatter
// block (opening `---` without a closing `---`) also returns ok=false.
//
// The frontmatter bytes contain only the YAML between the delimiters
// (delimiters excluded). The body bytes begin on the line after the
// closing `---`.
func splitFrontmatter(raw []byte) (fm []byte, body []byte, ok bool, warning string) {
	// Normalise CRLF → LF so the delimiter check works on Windows-
	// edited files.
	raw = bytes.ReplaceAll(raw, []byte("\r\n"), []byte("\n"))

	const delim = "---\n"
	const delimEnd = "---"

	var stripLen int
	if bytes.HasPrefix(raw, []byte(delim)) {
		stripLen = len(delim)
	} else if bytes.HasPrefix(raw, []byte(delimEnd)) {
		// Bare "---" at start of file (no trailing newline — edge case).
		// A 3-byte input that is exactly "---" has no closing delimiter.
		stripLen = len(delimEnd)
		if len(raw) <= stripLen {
			return nil, nil, false, "no closing '---'"
		}
	} else {
		return nil, nil, false, "no frontmatter delimiter found"
	}

	// Strip the opening delimiter.
	rest := raw[stripLen:]

	// Find the closing "---" on its own line.
	closeIdx := bytes.Index(rest, []byte("\n---\n"))
	if closeIdx == -1 {
		// Try closing at EOF: "...\n---" (no trailing newline).
		if bytes.HasSuffix(rest, []byte("\n---")) {
			closeIdx = len(rest) - len("\n---")
			fm = rest[:closeIdx]
			// body is empty after a "---" at EOF.
			return fm, []byte{}, true, ""
		}
		return nil, nil, false, "frontmatter opening '---' has no closing '---'"
	}

	fm = rest[:closeIdx]
	// body starts after "\n---\n" (skip the 5-byte sequence).
	bodyStart := closeIdx + len("\n---\n")
	if bodyStart > len(rest) {
		bodyStart = len(rest)
	}
	body = rest[bodyStart:]
	return fm, body, true, ""
}

// hasMachineLocalComment reports whether s contains the exact HTML
// comment <!-- meho:machine-local --> allowing arbitrary whitespace
// between the tokens (case-sensitive tag name).
//
// Valid forms:
//
//	<!-- meho:machine-local -->
//	<!--meho:machine-local-->
//	<!--  meho:machine-local  -->
func hasMachineLocalComment(s string) bool {
	const open = "<!--"
	const tag = "meho:machine-local"
	const close = "-->"

	idx := 0
	for {
		start := strings.Index(s[idx:], open)
		if start == -1 {
			return false
		}
		start += idx
		// Search for close strictly after the open delimiter so that a
		// degenerate comment like <!--> or <!--->, whose --> shares
		// characters with the opening <!--, cannot produce a negative
		// inner slice range and panic.
		innerStart := start + len(open)
		rel := strings.Index(s[innerStart:], close)
		if rel == -1 {
			return false
		}
		innerEnd := innerStart + rel
		if strings.TrimSpace(s[innerStart:innerEnd]) == tag {
			return true
		}
		idx = innerEnd + len(close)
	}
}

// sha256Hex returns the lowercase hex-encoded SHA-256 digest of b.
func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
