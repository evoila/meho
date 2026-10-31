// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// applyRequestContext is the root PersistentPreRunE. It puts the work
// reference and the current tenant into the command context, so every
// backplane request of the command sends the Meho-Work-Ref and
// Meho-Tenant headers.
func applyRequestContext(cmd *cobra.Command, args []string) error {
	if err := applyWorkRef(cmd, args); err != nil {
		return err
	}
	applyTenant(cmd)
	return nil
}

// applyTenant picks the tenant the command works in (#5241):
//
//  1. MEHO_TENANT, when set: a tenant name or ID for this one shell.
//  2. The tenant stored by `meho tenants use`, but only for the
//     backplane it was chosen on. A --backplane flag that names
//     another backplane sends no stored tenant.
//
// With neither, no header is sent: a login in one tenant works as
// before. The backplane checks the value on every request.
func applyTenant(cmd *cobra.Command) {
	if sendsNoBackplaneRequest(cmd) {
		return
	}
	if v := strings.TrimSpace(os.Getenv(api.TenantEnv)); v != "" {
		cmd.SetContext(api.WithTenant(cmd.Context(), v))
		return
	}
	cfg, err := auth.LoadConfig()
	if err != nil || cfg.Tenant == nil || cfg.Tenant.ID == "" {
		return
	}
	if f := cmd.Flags().Lookup("backplane"); f != nil && f.Changed && !sameBackplane(f.Value.String(), cfg.BackplaneURL) {
		return
	}
	cmd.SetContext(api.WithTenant(cmd.Context(), cfg.Tenant.ID))
}

// sameBackplane reports whether two backplane URLs name the same
// backplane once normalised.
func sameBackplane(a, b string) bool {
	na, errA := normalizeBackplaneURL(a)
	nb, errB := normalizeBackplaneURL(b)
	return errA == nil && errB == nil && na == nb
}
