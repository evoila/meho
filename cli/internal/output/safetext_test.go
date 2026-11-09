// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import "testing"

func TestSafeTextShowsControlCharactersAsEscapes(t *testing.T) {
	cases := []struct {
		name, in, want string
	}{
		{"plain text stays", "web-01 (vm-1042) Grüße 日本", "web-01 (vm-1042) Grüße 日本"},
		{"empty", "", ""},
		{"escape sequence", "a\x1b[1A\x1b[2Kb", `a\x1b[1A\x1b[2Kb`},
		{"carriage return", "safe\rdestructive", `safe\x0ddestructive`},
		{"bell", "x\a", `x\x07`},
		{"newline and tab", "a\nb\tc", `a\x0ab\x09c`},
		{"delete", "a\x7fb", `a\x7fb`},
		{"C1 control", "a\u009bb", `a\x9bb`},
		{"bidi override", "abc\u202edef", `abc\u202edef`},
		{"bidi isolate", "\u2066x\u2069", `\u2066x\u2069`},
		{"zero width space", "a\u200bb", `a\u200bb`},
		{"line separator", "a\u2028b", `a\u2028b`},
		{"invalid utf-8", "a\xffb", `a\xffb`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := SafeText(tc.in); got != tc.want {
				t.Errorf("SafeText(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

func TestSafeLinesKeepsLineBreaks(t *testing.T) {
	in := "{\n  \"name\": \"a\u202eb\"\n}\r"
	want := "{\n  \"name\": \"a\\u202eb\"\n}\\x0d"
	if got := SafeLines(in); got != want {
		t.Errorf("SafeLines(%q) = %q, want %q", in, got, want)
	}
	if got := SafeLines("one\ntwo"); got != "one\ntwo" {
		t.Errorf("SafeLines changed plain text: %q", got)
	}
}
