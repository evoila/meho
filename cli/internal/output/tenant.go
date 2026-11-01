// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"regexp"
	"strings"
)

// ErrCodeTenantNotChosen replaces the command's error code when the
// backplane answered 400 tenant_not_chosen (#5239): the login is in
// several tenants and the request named none. The exit code stays the
// one the command chose.
const ErrCodeTenantNotChosen = "tenant_not_chosen"

// TenantNotChosenHint is what the CLI says for a tenant_not_chosen
// answer (#5241).
const TenantNotChosenHint = "You are in several tenants. Pick one with `meho tenants use <name>`."

// tenantNamesRe finds the tenant list in the backplane's message:
// "tenant_not_chosen: this login is in 2 tenants (lab-a, lab-b). ..."
var tenantNamesRe = regexp.MustCompile(`tenant_not_chosen: this login is in \d+ tenants \(([^)]*)\)`)

// withTenantNotChosenHint returns err rewritten to the plain tenant_not_chosen
// message when its detail carries the backplane's tenant_not_chosen
// answer, else err unchanged. Every command renders backplane errors
// through RenderError, so this one place covers them all.
func withTenantNotChosenHint(err *StructuredError) *StructuredError {
	if !strings.Contains(err.Detail, "tenant_not_chosen") {
		return err
	}
	detail := TenantNotChosenHint
	if m := tenantNamesRe.FindStringSubmatch(err.Detail); m != nil {
		detail += " Your tenants: " + m[1] + "."
	}
	return &StructuredError{Code: ErrCodeTenantNotChosen, Detail: detail, Exit: err.Exit}
}
