// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package migrate — see doc.go for the package overview.
package migrate

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
)

// MachineLocalResult is the output of DetectMachineLocal.
// Flagged is true iff at least one rule fired; the caller
// (T4 picker) uses this to pre-mark a file "Skip — machine-local"
// while leaving the operator free to override.
type MachineLocalResult struct {
	Flagged bool
	Matches []MachineLocalMatch
}

// MachineLocalMatch describes one heuristic hit.
// Category is a stable string key identifying which rule fired;
// Sample is the matched substring truncated to ≤80 chars so the
// T4 picker badge tooltip stays readable without wrapping.
type MachineLocalMatch struct {
	Category string
	Sample   string
}

// staticRule bundles a compiled regex with its stable category name.
type staticRule struct {
	category string
	re       *regexp.Regexp
}

// staticRules holds rules that do not need runtime information.
var staticRules = []staticRule{
	{
		// unix-home: /Users/<seg>/ or /home/<seg>/
		// <seg> is one path segment (no additional slashes).
		category: "unix-home",
		re:       regexp.MustCompile(`(?i)/(?:Users|home)/[^/\s"'<>]+/`),
	},
	{
		// windows-home: C:\Users\<seg>\ (case-insensitive drive letter).
		// Double-backslash in the string encodes one literal backslash in
		// the regex pattern.
		category: "windows-home",
		re:       regexp.MustCompile(`(?i)[A-Za-z]:\\Users\\[^\\<>"'\s]+\\`),
	},
	{
		// tilde-home: ~/ token at a path boundary.
		// We require ~/  to be preceded by start-of-string, a newline /
		// tab, or a punctuation character that syntactically introduces a
		// path (quote, paren, equals, colon, comma).  A plain space mid-
		// sentence is intentionally excluded so "approx ~/2 days" (tilde
		// as a proximity marker) does not flag; ~/foo at line start or
		// after a quote does flag.
		category: "tilde-home",
		re:       regexp.MustCompile(`(?:^|[\n\r\t"'(=:,])~/`),
	},
	{
		// local-hostname: .local / .lan suffixes, and the bare tokens
		// localhost and host.docker.internal.
		// Alternation ordered longest-first per codebase convention.
		category: "local-hostname",
		re:       regexp.MustCompile(`(?i)\b(?:host\.docker\.internal|localhost|\S+\.local|\S+\.lan)\b`),
	},
}

// HomeDirFunc is the seam DetectMachineLocal uses to obtain the current
// user's home directory.  Production callers pass nil to use
// os.UserHomeDir; tests inject a deterministic fake to avoid depending
// on the CI runner's $HOME.
type HomeDirFunc func() (string, error)

// DetectMachineLocal applies all heuristic rules against body and
// returns a MachineLocalResult.  When homeFn is nil the function falls
// back to os.UserHomeDir.
func DetectMachineLocal(body string, homeFn HomeDirFunc) MachineLocalResult {
	if homeFn == nil {
		homeFn = os.UserHomeDir
	}

	var matches []MachineLocalMatch

	for _, r := range staticRules {
		for _, raw := range r.re.FindAllString(body, -1) {
			matches = append(matches, MachineLocalMatch{
				Category: r.category,
				Sample:   truncate(raw, 80),
			})
		}
	}

	matches = append(matches, detectUsername(body, homeFn)...)

	return MachineLocalResult{
		Flagged: len(matches) > 0,
		Matches: matches,
	}
}

// detectUsername applies the operator-username rule.
//
// The 3-occurrence threshold mirrors Initiative #375's spec (work-item
// 2, bullet 5).  A single occurrence of a short or common username
// in prose (e.g. "admin", "user") is not strong evidence of a
// machine-local path; three whole-word occurrences establish intent.
func detectUsername(body string, homeFn HomeDirFunc) []MachineLocalMatch {
	home, err := homeFn()
	if err != nil || home == "" {
		return nil
	}
	username := filepath.Base(home)
	if username == "" || username == "." || username == string(filepath.Separator) {
		return nil
	}

	// Use Unicode-aware boundaries instead of \b / \w which are ASCII-only
	// in Go's RE2 engine. \b would silently never match a username that
	// contains non-ASCII letters (e.g. "josé", "müller") because RE2's
	// \w == [0-9A-Za-z_], so the boundary test between é and a following
	// space sees two non-\w characters and no \w/\W transition.
	// (?:^|[^\pL\pN_]) matches either start-of-string or a Unicode
	// non-word character (not a Unicode letter, digit, or underscore).
	pattern := fmt.Sprintf(
		`(?i)(?:^|[^\pL\pN_])(%s)(?:[^\pL\pN_]|$)`,
		regexp.QuoteMeta(username),
	)
	re, err := regexp.Compile(pattern)
	if err != nil {
		return nil
	}

	hits := re.FindAllStringSubmatch(body, -1)
	if len(hits) < 3 {
		return nil
	}

	matches := make([]MachineLocalMatch, 0, len(hits))
	for _, h := range hits {
		if len(h) < 2 || h[1] == "" {
			continue
		}
		matches = append(matches, MachineLocalMatch{
			Category: "operator-username",
			Sample:   truncate(h[1], 80),
		})
	}
	return matches
}

// truncate returns s truncated to at most n runes.
func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}
