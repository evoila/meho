// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package admin hosts the `meho admin ...` parent command tree for
// deployer-side provisioning verbs. Unlike the rest of the CLI tree
// (which dispatches through the backplane), `admin` subcommands talk
// to upstream identity / secret stores directly using operator
// credentials — they are install-time bootstrap helpers, not
// agent-facing operations.
//
// The first member of this tree is `admin keycloak bootstrap-clients`
// (G0.9.1-T11, #791): provisions the public CLI device-code client +
// the public MCP client + their 5 protocol mappers + 4 default client
// scopes + a meho-admins group + an admin user against a Keycloak
// realm, idempotently. The verb encodes the 5-step recipe documented
// in deploy/values-examples/README.md § Auth onramp recipe.
//
// Future admin verbs (rotating chart-managed secrets, seeding initial
// tenants in MEHO's own DB, etc.) live here too — anything that the
// operator runs once at install time with credentials they hold but
// don't keep on the cluster.
package admin

import (
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/cmd/admin/keycloak"
)

// NewRootCmd returns the `meho admin` parent command, ready for
// grafting onto the top-level command tree by cmd/root.go. The parent
// takes no args and prints its own help; every piece of behaviour
// lives in subcommands.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "admin",
		Short: "Deployer-side install-time provisioning verbs",
		Long: "admin hosts install-time bootstrap helpers that talk to " +
			"upstream identity and secret stores directly using " +
			"operator credentials. Unlike the rest of the meho CLI, " +
			"these verbs do not dispatch through the backplane — they " +
			"provision the substrate the backplane needs to function " +
			"(public Keycloak clients, initial users, etc.).",
		SilenceUsage: true,
	}
	cmd.AddCommand(keycloak.NewRootCmd())
	return cmd
}
