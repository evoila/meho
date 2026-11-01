// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
)

// OperatorRoleNote is what `meho status` says when the login has the
// read_only role in the current tenant (#5241): the backplane answered
// the liveness route instead of the deep check, which stays behind the
// operator role.
const OperatorRoleNote = "  The other checks (credential store, readiness, approvals, features) need the operator role in this tenant."

// PrintLiveness renders the liveness answer (GET /api/v1/health/live)
// for `meho status`: the identity, the tenants with the current one
// marked, the database state, and the note that the deep checks need
// the operator role. The first lines match PrintHealth, so a person
// reads the same shape in every role.
func PrintLiveness(stdoutW io.Writer, resp *api.LivenessResponse) error {
	fmt.Fprintf(stdoutW, "Logged in as %s\n", formatOperatorIdentity(resp.Operator))
	fmt.Fprint(stdoutW, formatTenants(resp.Operator))
	fmt.Fprintf(stdoutW, "  DB:    %s\n", formatDB(resp.Db))
	fmt.Fprintln(stdoutW, OperatorRoleNote)
	return nil
}
