// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/text/unicode/norm"
)

// WorkRefHeader is the request header the backplane audit middleware
// reads to stamp audit_log.work_ref on the row for that request.
const WorkRefHeader = "Meho-Work-Ref"

// WorkRefEnv is the environment variable the root --work-ref flag
// falls back to when the flag is not set.
const WorkRefEnv = "MEHO_WORK_REF"

// WorkRefRule is the error text for a work-ref that ValidWorkRef
// refuses. The caller puts the flag or variable name in front of it.
const WorkRefRule = "must be a ticket id on one line, for example gh:evoila/meho#13 or CHG0012345"

// WorkRefMaxChars is the longest ticket id the backplane takes.
const WorkRefMaxChars = 256

// CleanWorkRef cleans ref like the backplane does: it removes white
// space at both ends and makes the text Unicode form NFC. White space is
// what Python's str.strip removes: the Unicode white space, and also the
// four information separators U+001C to U+001F. Send this value, not the
// raw text.
func CleanWorkRef(ref string) string {
	return norm.NFC.String(strings.TrimFunc(ref, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
	}))
}

// ValidWorkRef reports whether ref can be sent as a change-ticket
// reference. It refuses what the backplane refuses (the one rule in
// backend/src/meho_backplane/work_ref.py): a blank value, a value of more
// than 256 characters, and a value with a control character (also a tab
// or a line break), an invisible format character (zero-width and
// direction marks) or a line or paragraph separator. Text that is not
// valid UTF-8 is refused too, because the header carries UTF-8.
//
// The length is counted after cleaning, so a decomposed id that is longer
// than 256 characters only before the NFC step passes, as on the
// backplane. One thing differs from the backplane, on purpose. The CLI
// looks for refused characters before it removes white space. So it
// refuses a control character or a line or paragraph separator also at the
// start or the end of the id: a tab, a line feed, a vertical tab, a form
// feed, a carriage return (so also CR LF), U+001C to U+001F, U+0085,
// U+2028 and U+2029. The backplane strips these at the ends and
// takes the value. So the CLI is stricter than the backplane, never
// looser. Such a character in a flag or variable value is most likely a
// mistake.
// The shared case list
// testdata/work_ref_cases.json is read by this package's test and by the
// backend test, so the two rules cannot drift.
func ValidWorkRef(ref string) bool {
	if !utf8.ValidString(ref) || strings.ContainsFunc(ref, isRefusedWorkRefRune) {
		return false
	}
	cleaned := CleanWorkRef(ref)
	return cleaned != "" && utf8.RuneCountInString(cleaned) <= WorkRefMaxChars
}

func isRefusedWorkRefRune(r rune) bool {
	return unicode.In(r, unicode.Cc, unicode.Cf, unicode.Zl, unicode.Zp)
}

type workRefKey struct{}

// WithWorkRef returns a copy of ctx that carries ref. Requests built
// with that context get the Meho-Work-Ref header (see SetWorkRefHeader).
// An empty ref returns ctx unchanged.
func WithWorkRef(ctx context.Context, ref string) context.Context {
	if ref == "" {
		return ctx
	}
	return context.WithValue(ctx, workRefKey{}, ref)
}

// WorkRefFrom returns the work-ref carried by ctx, or "".
func WorkRefFrom(ctx context.Context) string {
	ref, _ := ctx.Value(workRefKey{}).(string)
	return ref
}

// SetWorkRefHeader sets the Meho-Work-Ref header on req when the
// request's context carries a work-ref. The AuthedClient request
// editor calls it for every generated-client call; hand-rolled
// backplane requests call it next to their Authorization header.
func SetWorkRefHeader(req *http.Request) {
	if ref := WorkRefFrom(req.Context()); ref != "" {
		req.Header.Set(WorkRefHeader, ref)
	}
}
