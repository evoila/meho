// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
	"regexp"
)

// WorkRefHeader is the request header the backplane audit middleware
// reads to stamp audit_log.work_ref on the row for that request.
const WorkRefHeader = "Meho-Work-Ref"

// WorkRefEnv is the environment variable the root --work-ref flag
// falls back to when the flag is not set.
const WorkRefEnv = "MEHO_WORK_REF"

var workRefPattern = regexp.MustCompile(`^gh:[^/[:space:]]+/[^#[:space:]]+#[1-9][0-9]*$`)

// ValidWorkRef reports whether ref has the gh:<owner>/<repo>#<n> shape.
func ValidWorkRef(ref string) bool {
	return workRefPattern.MatchString(ref)
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
