// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package output formats meho CLI command output. The package
// enforces Goal #11 spec section 5's output discipline contract:
//
//   - Default human-readable: prose summary on stdout, suitable for
//     a human operator's eyes.
//   - --json: a single JSON document on stdout, suitable for `jq`
//     post-processing and for agent / smoke-test consumers (the
//     install.sh dogfooding harness depends on this shape).
//   - Errors: structured with a stable string code, an exit code,
//     and a human or JSON rendering depending on the same --json
//     flag. Errors go to stderr; success output goes to stdout.
//
// The package deliberately stays free of cobra and api dependencies
// so the rendering surface is unit-testable against bytes.Buffer
// without spinning up a cobra command tree.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/evoila/meho/cli/internal/api"
)

// Exit codes per Goal #11 spec section 5. 0 is reserved for success
// (cobra's default behaviour when RunE returns nil); 1 is the
// generic "something went wrong" return main() uses when no
// StructuredError is present. The named codes start at 2 so the
// generic 1 stays distinguishable from a known failure mode.
const (
	// ExitAuthExpired indicates the operator's stored token is
	// missing, malformed, or rejected by the backplane (HTTP 401).
	// The remedy is always `meho login`.
	ExitAuthExpired = 2
	// ExitUnreachable indicates the CLI could not reach the
	// backplane at all — DNS failure, connection refused, TLS
	// handshake failure, request timeout. Distinguished from auth
	// failures because the remediation is different (check network,
	// not credentials).
	ExitUnreachable = 3
	// ExitUnexpected indicates the backplane responded but with a
	// shape the CLI couldn't decode, or with a status outside the
	// documented contract. Catches the "something changed on the
	// server side" failure mode without falling back to the generic
	// exit 1.
	ExitUnexpected = 4
	// ExitInsufficientRole indicates the backplane refused the
	// request on RBAC grounds (HTTP 403). The operator authenticated
	// successfully but their tenant role is below the minimum the
	// endpoint requires — `meho status --watch` against the SSE
	// feed needs operator role, read_only is rejected. Distinct
	// from auth_expired because `meho login` won't fix it; the
	// remedy is a tenant-admin role grant.
	//
	// Exit 5 is also overloaded for InsufficientBudget (see
	// ExitInsufficientBudget), since both states are
	// "authenticated, but the action couldn't complete because of
	// a configured limit the operator's tenant_admin must change."
	// The error code in the JSON envelope distinguishes the two.
	ExitInsufficientRole = 5
	// ExitInsufficientBudget indicates the operator's tenant has
	// over-budget operational conventions that exceed the preamble
	// token budget — some conventions will be dropped from agent
	// sessions until the tenant_admin resolves the overflow (raise
	// priority, shorten, or split entries). Surfaced by `meho
	// conventions list` (G7.1-T7 #1094) when the GET
	// /api/v1/conventions response carries non-empty
	// budget_status.dropped_slugs. Shares the integer value with
	// ExitInsufficientRole (both are "tenant_admin must act");
	// callers branch on the error code string, not the integer.
	ExitInsufficientBudget = 5
)

// Error codes (the "error" field in the JSON error envelope). The
// codes are short, machine-readable strings — the same strings
// jq-style consumers and dashboards filter on. Keep them stable
// across CLI releases.
const (
	// ErrCodeAuthExpired pairs with ExitAuthExpired. The operator
	// has no usable token; rerunning `meho login` is the remedy.
	ErrCodeAuthExpired = "auth_expired"
	// ErrCodeUnreachable pairs with ExitUnreachable. The backplane
	// could not be contacted; the operator should check network /
	// VPN / DNS.
	ErrCodeUnreachable = "unreachable"
	// ErrCodeUnexpected pairs with ExitUnexpected. The backplane
	// answered but the response shape was outside the contract.
	ErrCodeUnexpected = "unexpected_response"
	// ErrCodeInsufficientRole pairs with ExitInsufficientRole. The
	// operator is authenticated but their tenant role is below the
	// endpoint's minimum (HTTP 403). Remedy: tenant-admin role
	// grant, not a re-login.
	ErrCodeInsufficientRole = "insufficient_role"
	// ErrCodeInsufficientBudget pairs with ExitInsufficientBudget.
	// The operator's tenant has over-budget operational
	// conventions; some will be dropped from agent sessions until
	// the tenant_admin raises a priority, shortens a body, or
	// splits an entry. Surfaced by `meho conventions list` per
	// G7.1-T7 #1094.
	ErrCodeInsufficientBudget = "insufficient_budget"
)

// StructuredError is the error shape produced by every meho
// subcommand's RunE. Carries the operator-readable code, the exit
// code main() should propagate, and a free-form detail string.
// Implements the standard error interface so cobra's RunE plumbing
// surfaces the .Error() form to stderr; main() type-asserts to read
// the exit code.
//
// The Detail field is deliberately operator-facing: it explains
// *why* the failure happened in prose. Never embed a JWT, a refresh
// token, or any other credential into Detail — output discipline
// (Goal #11 §5) is mandatory and the sensitive-data discipline test
// (output_test.go) pins it.
type StructuredError struct {
	// Code is the stable machine-readable identifier
	// (auth_expired, unreachable, unexpected_response).
	Code string
	// Detail is the human-friendly prose appended after the code.
	// Safe to include URLs and HTTP statuses; never include tokens.
	Detail string
	// Exit is the process exit code main() will use when this
	// error reaches it. Set via the package-level constants
	// (ExitAuthExpired, ExitUnreachable, ExitUnexpected).
	Exit int
}

// Error renders the structured error for stderr. The format mirrors
// the `gh` and `flux` convention: a short prefix, the code, then
// the detail. cobra prints this verbatim via its SilenceErrors=false
// default, so the result file in `auto-implement-issue` runs
// captures this exact string when a status command fails.
func (e *StructuredError) Error() string {
	if e.Detail == "" {
		return fmt.Sprintf("meho: %s", e.Code)
	}
	return fmt.Sprintf("meho: %s: %s", e.Code, e.Detail)
}

// ExitCode returns the process exit code main() should propagate.
// Defined as a method so callers (main.go) can type-assert on the
// interface rather than depending on the concrete struct.
func (e *StructuredError) ExitCode() int { return e.Exit }

// ExitCoder is the interface main() type-asserts against. Any error
// returned from a RunE that satisfies ExitCoder gets its exit code
// honoured; anything else falls back to exit 1 (cobra's default for
// non-nil RunE returns).
type ExitCoder interface {
	error
	ExitCode() int
}

// jsonError is the on-the-wire shape emitted when --json is set
// and an error needs to surface. Stable across CLI releases —
// agent consumers (the install.sh smoke test) parse this directly.
type jsonError struct {
	Error    string `json:"error"`
	Detail   string `json:"detail,omitempty"`
	ExitCode int    `json:"exit_code"`
}

// AuthExpired builds the canonical auth_expired StructuredError.
// Detail is operator-facing; pass the underlying cause for context.
func AuthExpired(detail string) *StructuredError {
	return &StructuredError{
		Code:   ErrCodeAuthExpired,
		Detail: detail,
		Exit:   ExitAuthExpired,
	}
}

// Unreachable builds the canonical unreachable StructuredError.
func Unreachable(detail string) *StructuredError {
	return &StructuredError{
		Code:   ErrCodeUnreachable,
		Detail: detail,
		Exit:   ExitUnreachable,
	}
}

// Unexpected builds the canonical unexpected_response StructuredError.
func Unexpected(detail string) *StructuredError {
	return &StructuredError{
		Code:   ErrCodeUnexpected,
		Detail: detail,
		Exit:   ExitUnexpected,
	}
}

// InsufficientRole builds the canonical insufficient_role
// StructuredError for HTTP 403 responses. Detail should name the
// minimum required role so the operator can ask the right person
// for the grant.
func InsufficientRole(detail string) *StructuredError {
	return &StructuredError{
		Code:   ErrCodeInsufficientRole,
		Detail: detail,
		Exit:   ExitInsufficientRole,
	}
}

// InsufficientBudget builds the canonical insufficient_budget
// StructuredError for the over-budget preamble case (G7.1-T7 #1094).
// Detail should name the dropped slugs and the tenant's max / estimated
// token counts so the operator (or tenant_admin they alert) can act
// without re-running another command. The same exit code 5 surfaces as
// for insufficient_role; the error code string distinguishes them.
func InsufficientBudget(detail string) *StructuredError {
	return &StructuredError{
		Code:   ErrCodeInsufficientBudget,
		Detail: detail,
		Exit:   ExitInsufficientBudget,
	}
}

// RenderError prints the structured error in the chosen format and
// returns an error that propagates the exit code via ExitCoder.
//
// The human path writes "meho: <code>: <detail>\n" to stderrW and
// returns a silent ExitCoder so cobra's default printer doesn't
// double-render the message. The JSON path writes the
// {"error","detail","exit_code"} envelope to stderrW and likewise
// returns a silent ExitCoder. Both paths leave stdout untouched —
// success output goes to stdout, failure output to stderr, every
// time.
//
// Returning a silent ExitCoder (rather than the raw StructuredError)
// is what makes this function safe to call from a cobra RunE that
// also sets SilenceErrors=true: cobra sees a non-nil error and
// flips to a non-zero return without re-emitting any text.
func RenderError(stderrW io.Writer, err *StructuredError, jsonOut bool) error {
	if jsonOut {
		// JSON path: serialise the envelope to stderr ourselves.
		body := jsonError{
			Error:    err.Code,
			Detail:   err.Detail,
			ExitCode: err.Exit,
		}
		// Encoding can fail only on unsupported types, which the
		// jsonError shape doesn't have — but be defensive: a
		// rendering failure is itself an exception that the
		// operator deserves to see.
		blob, mErr := json.Marshal(body)
		if mErr != nil {
			// Fall back to the human path for the renderer's own
			// failure mode so the operator still gets actionable
			// feedback.
			fmt.Fprintf(stderrW, "meho: render error: %v\n", mErr)
			return &silentError{exit: err.Exit}
		}
		// One trailing newline so the JSON document doesn't merge
		// with whatever the parent shell prints next; agents that
		// pipe through jq are happy with the newline.
		fmt.Fprintln(stderrW, string(blob))
		return &silentError{exit: err.Exit}
	}
	// Human path: write the one-line "meho: <code>: <detail>"
	// rendering to stderr ourselves so we don't depend on cobra's
	// default printer (status.go runs with SilenceErrors=true so
	// the JSON envelope on the --json path stays uncontaminated).
	fmt.Fprintln(stderrW, err.Error())
	return &silentError{exit: err.Exit}
}

// silentError wraps an exit code without producing any printer-
// visible message. Used on the --json error path so cobra's
// SilenceErrors-style behaviour kicks in implicitly (we've already
// printed the JSON envelope to stderr) while main() still reads a
// non-zero exit code via the ExitCoder type-assertion.
type silentError struct{ exit int }

func (silentError) Error() string    { return "" }
func (e *silentError) ExitCode() int { return e.exit }

// PrintJSON marshals v to stdoutW as an indented JSON document
// followed by a single trailing newline. Indented because the
// canonical consumer is a human running `meho status --json`; the
// agent consumer (`install.sh`) pipes through `jq` which doesn't
// care about whitespace. Adding `--json --compact` is a v0.2
// enhancement.
func PrintJSON(stdoutW io.Writer, v any) error {
	blob, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("meho: marshal json: %w", err)
	}
	fmt.Fprintln(stdoutW, string(blob))
	return nil
}

// PrintHealth renders a HealthResponse for human consumption.
// Format is a stable key-value summary matching the convention
// `kubectl get`'s `-o wide` columns use: one line for the operator
// identity, indented lines for each subsystem. The exact format is
// part of the v0.1 contract — operators (and the install.sh dump
// log) scrape this; a re-layout in v0.2 would need a deprecation
// path.
//
// All fields are taken from the typed HealthResponse model so we
// surface only what the backplane intended to expose; the JWT
// bearer token (carried in the request, not the response) never
// touches this surface.
func PrintHealth(stdoutW io.Writer, resp *api.HealthResponse) error {
	// Operator identity. The backplane's HealthResponse.Operator
	// model exposes sub + optional name + optional email. We render
	// (name, email) when available; otherwise the sub alone, which
	// is the only field guaranteed by the backplane's JWT contract.
	identity := formatOperatorIdentity(resp.Operator)
	fmt.Fprintf(stdoutW, "Logged in as %s\n", identity)

	// Vault subsystem. reachable + read_ok plus optional detail.
	fmt.Fprintf(stdoutW, "  Vault: %s\n", formatVault(resp.Vault))

	// DB subsystem. migrated is a tri-state (true, false, nil/unknown).
	fmt.Fprintf(stdoutW, "  DB:    %s\n", formatDB(resp.Db))
	return nil
}

// formatOperatorIdentity collapses the OperatorIdentity model into a
// single-line operator string. Preference order: email, name, sub.
// The sub is always rendered as a trailing parenthesised hint so
// the operator can match it against IdP audit logs.
func formatOperatorIdentity(op api.OperatorIdentity) string {
	primary := op.Sub
	if op.Email != nil && *op.Email != "" {
		primary = *op.Email
	} else if op.Name != nil && *op.Name != "" {
		primary = *op.Name
	}
	if primary == op.Sub {
		return op.Sub
	}
	return fmt.Sprintf("%s (sub: %s)", primary, op.Sub)
}

// formatVault renders a VaultStatus as "reachable, read ok",
// "reachable, read failed (<detail>)", or "unreachable (<detail>)".
// The exact phrasing matches the issue body's example output.
func formatVault(v api.VaultStatus) string {
	if !v.Reachable {
		return joinDetail("unreachable", v.Detail)
	}
	if !v.ReadOk {
		return joinDetail("reachable, read failed", v.Detail)
	}
	return joinDetail("reachable, read OK", v.Detail)
}

// formatDB renders a DbStatus. Migrated == nil ("unknown") happens
// in the chassis stage before G2.3's migration probe is wired; the
// CLI surfaces that as-is so operators see the difference between
// "probed and failed" and "not probed yet".
func formatDB(db api.DbStatus) string {
	if db.Migrated == nil {
		return "unknown"
	}
	if *db.Migrated {
		return "migrated"
	}
	return "not migrated"
}

// joinDetail appends "(detail)" to a base label only when detail is
// non-nil and non-empty. Trims surrounding whitespace because some
// backplane detail strings are passed through from upstream errors
// that may carry trailing newlines.
func joinDetail(base string, detail *string) string {
	if detail == nil {
		return base
	}
	trimmed := strings.TrimSpace(*detail)
	if trimmed == "" {
		return base
	}
	return fmt.Sprintf("%s (%s)", base, trimmed)
}
