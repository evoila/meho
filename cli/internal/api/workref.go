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

// SessionIDHeader is the request header that carries the id of the
// login session. The backplane stores it as the session of the audit
// rows of the request.
const SessionIDHeader = "Meho-Session-Id"

// CommandHeader is the request header that carries the command path of
// the running CLI command without the program name, for example
// "vmware vm list". Only the command names are sent, never an argument
// or a flag. With AUDIT_SESSION_LINKS on, the backplane stores it on the
// audit rows of the request, so an uploaded agent session that ran the
// command can find its rows.
const CommandHeader = "Meho-Command"

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
// The length and the refused characters are checked after cleaning, as on
// the backplane. So a decomposed id that is longer than 256 characters only
// before the NFC step passes, and a control character or a line or
// paragraph separator at the start or the end is stripped and accepted.
//
// One thing differs from the backplane. The backplane checks with Unicode
// 16.0 and the CLI with the Unicode 15.0 tables of the Go toolchain. Both
// refuse the same characters. But the NFC step joins 20 letter pairs
// (Todhri, Tulu-Tigalari, Gurung Khema and Kirat Rai) only on the
// backplane, so the CLI cleans those ids to another text than the
// backplane does. The backplane joins them again when the id arrives. The
// CLI counts each such pair as two characters, so near the 256-character
// limit it can refuse an id made of these pairs that the backplane
// accepts. See docs/codebase/cli.md. The shared case list testdata/work_ref_cases.json
// is read by this package's test and by the backend test, so the two rules
// cannot drift.
func ValidWorkRef(ref string) bool {
	if !utf8.ValidString(ref) {
		return false
	}
	cleaned := CleanWorkRef(ref)
	if cleaned == "" || utf8.RuneCountInString(cleaned) > WorkRefMaxChars {
		return false
	}
	return !strings.ContainsFunc(cleaned, isRefusedWorkRefRune)
}

func isRefusedWorkRefRune(r rune) bool {
	return unicode.In(r, unicode.Cc, unicode.Cf, unicode.Zl, unicode.Zp)
}

type workRefKey struct{}

type commandKey struct{}

// WithCommand returns a copy of ctx that carries the command path
// (see CommandHeader). An empty path returns ctx unchanged.
func WithCommand(ctx context.Context, path string) context.Context {
	if path == "" {
		return ctx
	}
	return context.WithValue(ctx, commandKey{}, path)
}

// SetCommandHeader sets the Meho-Command header on req when the
// request's context carries a command path. The AuthedClient request
// editor calls it for every generated-client call; hand-rolled backplane
// requests call it next to SetWorkRefHeader.
func SetCommandHeader(req *http.Request) {
	if path, _ := req.Context().Value(commandKey{}).(string); path != "" {
		req.Header.Set(CommandHeader, path)
	}
}

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

// SetSessionIDHeader sets the Meho-Session-Id header on req when
// sessionID is not empty. The AuthedClient request editor calls it for
// every generated-client call; hand-rolled backplane requests call it
// next to their Authorization header, with AuthedClient.SessionID.
func SetSessionIDHeader(req *http.Request, sessionID string) {
	if sessionID != "" {
		req.Header.Set(SessionIDHeader, sessionID)
	}
}
