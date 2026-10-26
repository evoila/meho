// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import "github.com/spf13/cobra"

// newDeleteCmd returns the `meho connector delete` command.
//
// CLI shape:
//
//	meho connector delete <connector_id> [--confirm] [--json] [--backplane <url>]
//
// Hits DELETE /api/v1/connectors/<connector_id>. The route deletes in
// the calling operator's tenant only: it removes the tenant's
// operation and group rows for the connector and writes one audit
// row. A built-in copy of the same connector is never touched. When a
// tenant has an old copy stored under a former product name next to a
// copy under the current product (#4889), the old copy goes first, so
// the delete hint on `meho connector list` is safe to follow.
//
// The verb reuses the enable / disable transition machinery: same
// flags, same confirmation prompt, same 204 success shape.
func newDeleteCmd() *cobra.Command {
	return newTransitionCmd(transitionParams{
		Verb:   verbDelete,
		Short:  "Remove a connector from your tenant (its operations and groups; the built-in copy stays)",
		Action: "deleted",
		ConfirmQuestion: "Delete connector %s from your tenant — its operations and groups are removed. " +
			"A built-in copy is not affected. Continue?",
		Long: "delete calls DELETE /api/v1/connectors/<connector_id>.\n\n" +
			"It removes the connector's operations and groups from your own tenant\n" +
			"and writes one audit row. A built-in copy of the connector is never\n" +
			"touched. If your tenant has an old copy stored under a former product\n" +
			"name next to a copy under the current product, the old copy is removed\n" +
			"first; run the command again to remove the current copy.\n\n" +
			"The route is not idempotent: once the connector is gone, a repeat call\n" +
			"answers 404.\n\n" +
			"Without --confirm, the verb prompts on stdin for confirmation;\n" +
			"--confirm skips the prompt for scripted use (CI pipelines, etc.).\n\n" +
			"Role: tenant_admin.",
	})
}
