// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"regexp"
	"sort"
	"strings"
)

// BIND key secrets (`secret "<base64>";` and `cookie-secret "<hex>";`),
// found the way the server finds them
// (backend/src/meho_backplane/redaction/bind_secret.py). The text is cut
// into comments, quoted strings and code the way BIND reads it. The
// keyword counts in code and in comments, never in a string. Only the
// value is reported, so the keyword and the comments stay readable.

type bindScope struct {
	kind       int
	start, end int
}

const (
	bindCode = iota
	bindComment
	bindString
)

var (
	bindPieceStart = regexp.MustCompile(`/\*|//|#|"`)
	bindStringEnd  = regexp.MustCompile(`^(?:[^"\\]|\\[\s\S])*"`)
	bindKeyword    = regexp.MustCompile(`(?i)(?:cookie-)?secret`)
	// The value patterns have no upper bound here (Go regexps allow at
	// most 1000 repeats); bindValueMax is checked after the match.
	bindQuotedValue  = regexp.MustCompile(`^"[A-Za-z0-9+/=\s]+"$`)
	bindBareValue    = regexp.MustCompile(`^[^\s;{}/*"!#]+`)
	bindCommentValue = regexp.MustCompile(`^(?:\s*("[A-Za-z0-9+/=\s]+")|\s+([^\s;{}/*"!#]+))`)
	bindSpaceAndSemi = regexp.MustCompile(`^\s*;`)
)

const bindValueMax = 4096

// isBindBoundary says whether b may sit right before or after the keyword.
func isBindBoundary(b byte) bool {
	return strings.IndexByte(" \t\n\v\f\r{};!/\"#", b) >= 0
}

// bindSecretSpans returns the start and end of every BIND key secret
// value in text, left to right.
func bindSecretSpans(text string) [][2]int {
	scopes := bindSplit(text)
	var found [][2]int
	for i, sc := range scopes {
		if sc.kind == bindString {
			continue
		}
		for _, kw := range bindKeyword.FindAllStringIndex(text[sc.start:sc.end], -1) {
			start, end := sc.start+kw[0], sc.start+kw[1]
			if start > 0 && !isBindBoundary(text[start-1]) || end < sc.end && !isBindBoundary(text[end]) {
				continue
			}
			var span *[2]int
			if sc.kind == bindComment {
				span = bindValueInComment(text, end, sc.end)
			} else {
				span = bindValueInCode(text, scopes, i, end)
			}
			if span != nil {
				found = append(found, *span)
			}
		}
	}
	// A value in code can sit after a comment with a secret of its own,
	// so sort the values and keep each one that does not overlap.
	sort.Slice(found, func(a, b int) bool {
		if found[a][0] != found[b][0] {
			return found[a][0] < found[b][0]
		}
		return found[a][1] < found[b][1]
	})
	var spans [][2]int
	lastEnd := 0
	for _, s := range found {
		if s[0] >= lastEnd {
			spans = append(spans, s)
			lastEnd = s[1]
		}
	}
	return spans
}

// bindSplit cuts text into code, comment and string scopes that cover it
// whole. A mark that never closes counts as code, as do all later marks
// of its kind.
func bindSplit(text string) []bindScope {
	var scopes []bindScope
	codeStart, cursor := 0, 0
	unclosed := map[string]bool{}
	for cursor < len(text) {
		loc := bindPieceStart.FindStringIndex(text[cursor:])
		if loc == nil {
			break
		}
		start := cursor + loc[0]
		mark := text[start : cursor+loc[1]]
		end := -1
		if !unclosed[mark] {
			end = bindPieceEnd(text, start, mark)
		}
		if end < 0 {
			unclosed[mark] = true
			cursor = start + 1
			continue
		}
		if start > codeStart {
			scopes = append(scopes, bindScope{bindCode, codeStart, start})
		}
		kind := bindComment
		if mark == `"` {
			kind = bindString
		}
		scopes = append(scopes, bindScope{kind, start, end})
		codeStart, cursor = end, end
	}
	if codeStart < len(text) {
		scopes = append(scopes, bindScope{bindCode, codeStart, len(text)})
	}
	return scopes
}

func bindPieceEnd(text string, start int, mark string) int {
	switch mark {
	case "/*":
		if i := strings.Index(text[start+2:], "*/"); i >= 0 {
			return start + 2 + i + 2
		}
		return -1
	case `"`:
		if loc := bindStringEnd.FindStringIndex(text[start+1:]); loc != nil {
			return start + 1 + loc[1]
		}
		return -1
	}
	if i := strings.IndexByte(text[start:], '\n'); i >= 0 {
		return start + i
	}
	return len(text)
}

func skipSpace(text string, pos, end int) int {
	for pos < end && strings.IndexByte(" \t\n\v\f\r", text[pos]) >= 0 {
		pos++
	}
	return pos
}

func bindValueInComment(text string, pos, end int) *[2]int {
	m := bindCommentValue.FindStringSubmatchIndex(text[pos:end])
	if m == nil {
		return nil
	}
	if m[2] >= 0 {
		if m[3]-m[2]-2 > bindValueMax {
			return nil
		}
		return &[2]int{pos + m[2], pos + m[3]}
	}
	if m[5]-m[4] > bindValueMax || !bindSpaceAndSemi.MatchString(text[pos+m[5]:end]) {
		return nil
	}
	return &[2]int{pos + m[4], pos + m[5]}
}

func bindValueInCode(text string, scopes []bindScope, index, pos int) *[2]int {
	sc := scopes[index]
	kind, end := sc.kind, sc.end
	for kind != bindString {
		if kind == bindCode {
			pos = skipSpace(text, pos, end)
			if pos < end {
				return bindBareValueAt(text, scopes, index, pos)
			}
		}
		index++
		if index == len(scopes) {
			return nil
		}
		kind, pos, end = scopes[index].kind, scopes[index].start, scopes[index].end
	}
	if end-pos-2 > bindValueMax || !bindQuotedValue.MatchString(text[pos:end]) {
		return nil
	}
	return &[2]int{pos, end}
}

// bindBareValueAt reads a value without quotes; ";" or a comment must follow.
func bindBareValueAt(text string, scopes []bindScope, index, pos int) *[2]int {
	end := scopes[index].end
	loc := bindBareValue.FindStringIndex(text[pos:end])
	if loc == nil {
		return nil
	}
	valueEnd := pos + loc[1]
	if loc[1] > bindValueMax {
		valueEnd = pos + bindValueMax
	}
	after := skipSpace(text, valueEnd, end)
	if after < end {
		if text[after] == ';' {
			return &[2]int{pos, valueEnd}
		}
		return nil
	}
	if index+1 < len(scopes) && scopes[index+1].kind == bindComment {
		return &[2]int{pos, valueEnd}
	}
	return nil
}
