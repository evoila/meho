// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

// Canonical scope tokens shared by the T4 picker and T5 submission.
// Define them here so there is one source of truth and callers can
// compare with == rather than with magic strings.
const (
	// ScopeUser is the safest default — entry visible only to the
	// authenticated user across all tenants they belong to.
	ScopeUser = "user"

	// ScopeUserTenant scopes the entry to the user within a specific
	// tenant. Suggested for `type: project` when a tenant backplane is
	// configured.
	ScopeUserTenant = "user×tenant"

	// ScopeUserTarget scopes the entry to the user within a specific
	// target system.
	ScopeUserTarget = "user×target"

	// ScopeTenant scopes the entry to the entire tenant (all users).
	ScopeTenant = "tenant"

	// ScopeTarget scopes the entry to a specific target system
	// (all users in the tenant).
	ScopeTarget = "target"
)

// SuggestScope returns the recommended canonical scope token for m
// based on its frontmatter type and whether a tenant backplane is
// configured.
//
// Mapping table (deterministic; no AI inference — see Initiative #375
// and ai_engineering_best_practices §Architecture-&-boundaries):
//
//   - type "user" or "feedback" → ScopeUser
//   - type "project"            → ScopeUserTenant if tenantConfigured, else ScopeUser
//   - type "reference"          → ScopeUser
//   - any other / empty type    → ScopeUser (safest default; T4 surfaces as overridable)
//
// The returned value is always one of the exported Scope* constants.
func SuggestScope(m MemoryFile, tenantConfigured bool) string {
	switch m.Type {
	case "user", "feedback":
		return ScopeUser
	case "project":
		if tenantConfigured {
			return ScopeUserTenant
		}
		return ScopeUser
	case "reference":
		return ScopeUser
	default:
		return ScopeUser
	}
}
