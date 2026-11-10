// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"fmt"
	"strings"
	"unicode"
	"unicode/utf8"
)

// SafeText returns s ready to print on a terminal. Text from the server
// (a request subject, a summary, a name) is shown as it is, except for
// characters a terminal does not print as text: C0 and C1 control
// characters (ESC, CR, BEL, newline, tab, ...), DEL, Unicode format
// characters (the bidi controls, zero-width characters, ...), the line
// and paragraph separators, and bytes that are not valid UTF-8. Each of
// those is shown as a visible escape: `\x1b` for a byte or a code point
// below U+0100, `‮` for a larger code point. So the text cannot move
// the cursor, overwrite a line, or change the order in which a line reads.
func SafeText(s string) string {
	if !needsEscape(s) {
		return s
	}
	var b strings.Builder
	b.Grow(len(s) + 8)
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		switch {
		case r == utf8.RuneError && size == 1:
			fmt.Fprintf(&b, `\x%02x`, s[i])
		case unprintable(r) && r < 0x100:
			fmt.Fprintf(&b, `\x%02x`, r)
		case unprintable(r):
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			b.WriteRune(r)
		}
		i += size
	}
	return b.String()
}

// SafeLines is SafeText for text of more than one line: each line is
// escaped and the newlines between them stay. Use it for a block that is
// meant to span lines, such as indented JSON or an error message.
func SafeLines(s string) string {
	if !needsEscape(s) {
		return s
	}
	lines := strings.Split(s, "\n")
	for i, line := range lines {
		lines[i] = SafeText(line)
	}
	return strings.Join(lines, "\n")
}

func needsEscape(s string) bool {
	for i, r := range s {
		if r == utf8.RuneError {
			if _, size := utf8.DecodeRuneInString(s[i:]); size == 1 {
				return true
			}
		}
		if unprintable(r) {
			return true
		}
	}
	return false
}

// unprintable reports whether r is a control character (Cc: C0, DEL, C1),
// a format character (Cf: bidi controls, zero-width characters), or a
// line or paragraph separator (Zl, Zp).
func unprintable(r rune) bool {
	return unicode.Is(unicode.Cc, r) || unicode.Is(unicode.Cf, r) ||
		unicode.Is(unicode.Zl, r) || unicode.Is(unicode.Zp, r)
}
