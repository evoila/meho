// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
	"strings"
	"unicode"
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

// ValidWorkRef reports whether ref can be sent as a change-ticket
// reference. The backplane takes any non-empty text: a GitHub issue
// (gh:<owner>/<repo>#<n>), a Jira key, a ServiceNow change number.
// So the CLI only refuses what the backplane or the header cannot
// carry: blank text, and control characters such as a line break. A
// tab is allowed, like in an HTTP header.
func ValidWorkRef(ref string) bool {
	if strings.TrimSpace(ref) == "" {
		return false
	}
	return !strings.ContainsFunc(ref, func(r rune) bool {
		return r != '\t' && unicode.IsControl(r)
	})
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
